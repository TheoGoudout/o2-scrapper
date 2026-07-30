"""Tests for the Google Calendar mapping and the sync decision engine.

Nothing here touches the network. The decision engine is pure, and the calendar
client is exercised against a fake discovery service, so every path that could
destroy data — deletion in particular — is covered offline.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from o2sync import gcal, sync
from o2sync.errors import CalendarAPIError
from o2sync.model import PARIS, normalize_event, reset_warnings
from tests.test_model import epoch_ms, make_event

WINDOW_START = datetime(2026, 6, 1, tzinfo=PARIS)
WINDOW_END = datetime(2026, 9, 1, tzinfo=PARIS)


def service(**overrides):
    return normalize_event(make_event(**overrides))


def synced_event(svc, *, content_hash=None, calendar_id=None, start=None):
    """A Google event as it would look after we synced ``svc``."""
    body = gcal.build_event_body(svc)
    event = {
        "id": calendar_id or body["id"],
        "summary": body["summary"],
        "start": start or body["start"],
        "end": body["end"],
        "extendedProperties": body["extendedProperties"],
    }
    if content_hash is not None:
        event["extendedProperties"]["private"][gcal.PROP_HASH] = content_hash
    return event


class EventIdTest(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(gcal.event_id_for("730187166"), gcal.event_id_for("730187166"))

    def test_is_distinct_per_service(self):
        self.assertNotEqual(gcal.event_id_for("1"), gcal.event_id_for("2"))

    def test_respects_googles_base32hex_charset(self):
        # Google rejects ids outside [a-v0-9]{5,1024}; a hex digest is a subset.
        for o2_id in ["1", "730187166", "abc-def", "", "é"]:
            with self.subTest(o2_id=o2_id):
                event_id = gcal.event_id_for(o2_id)
                self.assertRegex(event_id, r"^[a-v0-9]{5,1024}$")


class EventBodyTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()

    def test_title_is_service_then_intervenant(self):
        body = gcal.build_event_body(service())
        self.assertEqual(body["summary"], "Ménage - repassage — Camille")

    def test_cancelled_title_is_prefixed(self):
        body = gcal.build_event_body(service(eventStatus="annul_cli_ok"))
        self.assertEqual(body["summary"], "[ANNULÉ] Ménage - repassage — Camille")

    def test_times_carry_the_paris_timezone(self):
        body = gcal.build_event_body(service())
        self.assertEqual(body["start"]["timeZone"], "Europe/Paris")
        self.assertTrue(body["start"]["dateTime"].startswith("2026-06-30T11:34:00"))
        self.assertTrue(body["end"]["dateTime"].startswith("2026-06-30T13:34:00"))

    def test_carries_our_marker_and_hash(self):
        svc = service()
        private = gcal.build_event_body(svc)["extendedProperties"]["private"]
        self.assertEqual(private[gcal.PROP_SOURCE], gcal.SOURCE)
        self.assertEqual(private[gcal.PROP_EVENT_ID], svc.event_id)
        self.assertEqual(private[gcal.PROP_HASH], svc.content_hash())
        self.assertEqual(private[gcal.PROP_STATUS], "evt_status_done")

    def test_location_comes_from_the_city(self):
        self.assertEqual(gcal.build_event_body(service())["location"], "Lyon 1er Arrondissement")

    def test_cancelled_events_are_free_grey_and_silent(self):
        body = gcal.build_event_body(service(eventStatus="annul_cli_HD"))
        self.assertEqual(body["transparency"], "transparent")
        self.assertEqual(body["colorId"], gcal.CANCELLED_COLOUR_ID)
        self.assertEqual(body["reminders"], {"useDefault": False, "overrides": []})

    def test_active_events_are_busy_and_keep_default_reminders(self):
        body = gcal.build_event_body(service())
        self.assertEqual(body["transparency"], "opaque")
        self.assertNotIn("reminders", body)
        self.assertEqual(body["colorId"], gcal.CATEGORY_COLOURS["menage"])

    def test_description_mentions_status_and_source(self):
        body = gcal.build_event_body(service(eventStatus="annul_vac_scol", eventPostponed=True))
        self.assertIn("Annulée pour cause de vacances scolaires", body["description"])
        self.assertIn("reprogrammée", body["description"])
        self.assertIn("client.o2.fr", body["description"])


class PlanTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()

    def test_creates_an_event_for_a_new_service(self):
        result = sync.plan([service()], [], WINDOW_START, WINDOW_END)
        self.assertEqual(len(result.create), 1)
        self.assertEqual(result.summary(), "1 created, 0 updated, 0 unchanged, 0 deleted")

    def test_preview_label_matches_the_real_calendar_title(self):
        # A --dry-run that reads differently from what gets written is useless.
        svc = service()
        result = sync.plan([svc], [], WINDOW_START, WINDOW_END)
        self.assertIn(gcal.build_event_body(svc)["summary"], result.create[0].label)

    def test_leaves_an_unchanged_service_alone(self):
        svc = service()
        result = sync.plan([svc], [synced_event(svc)], WINDOW_START, WINDOW_END)
        self.assertEqual(len(result.unchanged), 1)
        self.assertTrue(result.is_empty)

    def test_updates_rather_than_recreates_a_rescheduled_service(self):
        before = service()
        after = service(eventStart=epoch_ms(2026, 6, 30, 15, 0))
        existing = synced_event(before)

        result = sync.plan([after], [existing], WINDOW_START, WINDOW_END)

        self.assertEqual(len(result.create), 0)
        self.assertEqual(len(result.delete), 0)
        self.assertEqual(len(result.update), 1)
        # The very point of the exercise: same calendar event, new content.
        self.assertEqual(result.update[0].calendar_event_id, existing["id"])

    def test_updates_a_service_that_became_cancelled(self):
        before = service()
        after = service(eventStatus="annul_cli_ok")

        result = sync.plan([after], [synced_event(before)], WINDOW_START, WINDOW_END)

        self.assertEqual(len(result.update), 1)
        self.assertTrue(result.update[0].body["summary"].startswith("[ANNULÉ]"))
        self.assertEqual(result.update[0].body["transparency"], "transparent")

    def test_updates_when_the_stored_hash_is_missing(self):
        svc = service()
        stale = synced_event(svc)
        del stale["extendedProperties"]["private"][gcal.PROP_HASH]

        result = sync.plan([svc], [stale], WINDOW_START, WINDOW_END)

        self.assertEqual(len(result.update), 1)
        self.assertEqual(result.update[0].reason, "no stored hash")

    def test_deletes_a_service_that_vanished_from_o2(self):
        gone = service()
        result = sync.plan([], [synced_event(gone)], WINDOW_START, WINDOW_END)

        self.assertEqual(len(result.delete), 1)
        self.assertIn("no longer in the O2 planning", result.delete[0].reason)

    def test_never_deletes_outside_the_window(self):
        # A narrow --start/--end run must not wipe services it did not ask about.
        outside = service(eventStart=epoch_ms(2026, 12, 25, 9, 0))
        result = sync.plan([], [synced_event(outside)], WINDOW_START, WINDOW_END)
        self.assertEqual(result.delete, [])

    def test_ignores_events_that_are_not_ours(self):
        foreign = {
            "id": "somebodyelsesevent",
            "summary": "Dentist",
            "start": {"dateTime": "2026-06-30T11:00:00+02:00"},
        }
        result = sync.plan([], [foreign], WINDOW_START, WINDOW_END)
        self.assertTrue(result.is_empty)

    def test_ignores_our_events_with_an_unreadable_start(self):
        svc = service()
        broken = synced_event(svc, start={"dateTime": "not-a-date"})
        result = sync.plan([], [broken], WINDOW_START, WINDOW_END)
        self.assertEqual(result.delete, [])

    def test_removes_duplicate_events_for_one_service(self):
        svc = service()
        first = synced_event(svc, calendar_id="firstcopy00000")
        duplicate = synced_event(svc, calendar_id="secondcopy0000")

        result = sync.plan([svc], [first, duplicate], WINDOW_START, WINDOW_END)

        self.assertEqual(len(result.unchanged), 1)
        self.assertEqual(len(result.delete), 1)
        self.assertEqual(result.delete[0].calendar_event_id, "secondcopy0000")

    def test_handles_an_all_day_existing_event(self):
        svc = service()
        all_day = synced_event(svc, start={"date": "2026-06-30"})
        result = sync.plan([], [all_day], WINDOW_START, WINDOW_END)
        self.assertEqual(len(result.delete), 1)

    def test_mixed_run(self):
        unchanged = service(eventId=1, eventStart=epoch_ms(2026, 6, 2, 9, 0))
        changed_before = service(eventId=2, eventStart=epoch_ms(2026, 6, 9, 9, 0))
        changed_after = service(eventId=2, eventStart=epoch_ms(2026, 6, 9, 14, 0))
        new = service(eventId=3, eventStart=epoch_ms(2026, 6, 16, 9, 0))
        vanished = service(eventId=4, eventStart=epoch_ms(2026, 6, 23, 9, 0))

        existing = [synced_event(unchanged), synced_event(changed_before), synced_event(vanished)]
        result = sync.plan([unchanged, changed_after, new], existing, WINDOW_START, WINDOW_END)

        self.assertEqual(result.summary(), "1 created, 1 updated, 1 unchanged, 1 deleted")


class FakeRequest:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeEvents:
    """Minimal stand-in for ``service.events()``."""

    def __init__(self, store, fail_insert_with=None):
        self.store = store
        self.fail_insert_with = fail_insert_with
        self.calls = []

    def insert(self, calendarId, body):
        def run():
            self.calls.append(("insert", body["id"]))
            if self.fail_insert_with is not None:
                raise self.fail_insert_with
            self.store[body["id"]] = body
            return body

        return FakeRequest(run)

    def update(self, calendarId, eventId, body):
        def run():
            self.calls.append(("update", eventId))
            self.store[eventId] = body
            return body

        return FakeRequest(run)

    def delete(self, calendarId, eventId):
        def run():
            self.calls.append(("delete", eventId))
            self.store.pop(eventId, None)
            return None

        return FakeRequest(run)


class FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def http_error(status):
    from googleapiclient.errors import HttpError

    return HttpError(SimpleNamespace(status=status, reason="fake"), b"{}")


class CalendarClientTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()
        self.store = {}
        self.events = FakeEvents(self.store)
        self.client = gcal.CalendarClient(service=FakeService(self.events))

    def test_create_inserts(self):
        body = gcal.build_event_body(service())
        self.client.create_event("cal", body)
        self.assertIn(body["id"], self.store)
        self.assertEqual(self.events.calls, [("insert", body["id"])])

    def test_create_falls_back_to_update_on_a_tombstoned_id(self):
        # Deleting a Google event reserves its id forever, so a service that was
        # removed from O2 and came back must not fail to insert.
        body = gcal.build_event_body(service())
        events = FakeEvents(self.store, fail_insert_with=http_error(409))
        client = gcal.CalendarClient(service=FakeService(events))

        client.create_event("cal", body)

        self.assertEqual(events.calls, [("insert", body["id"]), ("update", body["id"])])
        self.assertIn(body["id"], self.store)

    def test_create_reports_other_http_errors(self):
        body = gcal.build_event_body(service())
        events = FakeEvents(self.store, fail_insert_with=http_error(403))
        client = gcal.CalendarClient(service=FakeService(events))

        with self.assertRaises(CalendarAPIError):
            client.create_event("cal", body)

    def test_delete_tolerates_an_already_deleted_event(self):
        class Missing(FakeEvents):
            def delete(self, calendarId, eventId):
                def run():
                    raise http_error(410)

                return FakeRequest(run)

        client = gcal.CalendarClient(service=FakeService(Missing(self.store)))
        client.delete_event("cal", "gone")  # must not raise


class ApplyTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()
        self.store = {}
        self.events = FakeEvents(self.store)
        self.client = gcal.CalendarClient(service=FakeService(self.events))

    def _mixed_plan(self):
        changed_before = service(eventId=2, eventStart=epoch_ms(2026, 6, 9, 9, 0))
        changed_after = service(eventId=2, eventStart=epoch_ms(2026, 6, 9, 14, 0))
        new = service(eventId=3, eventStart=epoch_ms(2026, 6, 16, 9, 0))
        vanished = service(eventId=4, eventStart=epoch_ms(2026, 6, 23, 9, 0))
        existing = [synced_event(changed_before), synced_event(vanished)]
        return sync.plan([changed_after, new], existing, WINDOW_START, WINDOW_END)

    def test_dry_run_writes_nothing(self):
        plan = self._mixed_plan()
        self.assertFalse(plan.is_empty)

        sync.apply(plan, self.client, "cal", dry_run=True)

        self.assertEqual(self.events.calls, [])
        self.assertEqual(self.store, {})

    def test_apply_performs_every_kind_of_write(self):
        plan = self._mixed_plan()
        sync.apply(plan, self.client, "cal", dry_run=False)

        kinds = [kind for kind, _ in self.events.calls]
        self.assertEqual(sorted(kinds), ["delete", "insert", "update"])


class CalendarResolutionTest(unittest.TestCase):
    """The calendar.app.created scope can create a calendar but cannot list any.

    calendarList.list requires calendar.readonly / calendar / calendar.calendarlist*,
    so a real deployment gets a 403 there. Discovery must therefore be optional and
    a missing id must never silently create a duplicate calendar on every run.
    """

    class FakeCalendarList:
        def __init__(self, items, error=None):
            self.items = items
            self.error = error

        def list(self, pageToken=None, showHidden=False):
            def run():
                if self.error:
                    raise self.error
                return {"items": self.items}

            return FakeRequest(run)

    class FakeCalendars:
        def __init__(self):
            self.created = []

        def insert(self, body):
            def run():
                self.created.append(body)
                return {"id": "newcalendarid", **body}

            return FakeRequest(run)

    def _client(self, items, error=None):
        calendars = self.FakeCalendars()
        calendar_list = self.FakeCalendarList(items, error)
        service_obj = SimpleNamespace(
            calendarList=lambda: calendar_list, calendars=lambda: calendars
        )
        return gcal.CalendarClient(service=service_obj), calendars

    def test_an_explicit_id_skips_discovery_entirely(self):
        client, calendars = self._client([], error=http_error(403))
        self.assertEqual(client.resolve_calendar("O2 – Prestations", "given-id"), "given-id")
        self.assertEqual(calendars.created, [])

    def test_discovery_is_used_when_the_scope_allows_it(self):
        client, calendars = self._client([{"id": "existing", "summary": "O2 – Prestations"}])
        self.assertEqual(client.resolve_calendar("O2 – Prestations"), "existing")
        self.assertEqual(calendars.created, [])

    def test_forbidden_listing_asks_for_calendar_init_instead_of_creating(self):
        # The bug this replaces: falling through to create() here would have made a
        # brand new calendar on every single sync cycle.
        client, calendars = self._client([], error=http_error(403))
        with self.assertRaises(CalendarAPIError) as caught:
            client.resolve_calendar("O2 – Prestations")
        self.assertIn("calendar-init", str(caught.exception))
        self.assertEqual(calendars.created, [])

    def test_missing_calendar_with_listing_allowed_still_refuses_to_guess(self):
        client, calendars = self._client([{"id": "other", "summary": "Work"}])
        with self.assertRaises(CalendarAPIError):
            client.resolve_calendar("O2 – Prestations")
        self.assertEqual(calendars.created, [])

    def test_other_listing_errors_are_not_swallowed(self):
        client, _ = self._client([], error=http_error(500))
        with self.assertRaises(CalendarAPIError):
            client.find_calendar_by_name("O2 – Prestations")

    def test_create_calendar_uses_the_paris_timezone(self):
        client, calendars = self._client([])
        self.assertEqual(client.create_calendar("O2 – Prestations"), "newcalendarid")
        self.assertEqual(calendars.created[0]["timeZone"], "Europe/Paris")


if __name__ == "__main__":
    unittest.main()
