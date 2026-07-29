"""Tests for the normalisation layer.

The fixtures mirror the shape of a real ``get_planning_events`` response (verified
against a live account) but every value is synthetic — no personal data lives in
this repository.
"""

import unittest
from datetime import datetime, timedelta

from o2sync.model import (
    CANCELLED_COLOUR,
    PARIS,
    UNKNOWN_STATUS_LABEL,
    normalize_all,
    normalize_event,
    reset_warnings,
    to_document,
)


def epoch_ms(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=PARIS).timestamp()) * 1000


def make_event(**overrides):
    event = {
        "eventId": 730187166,
        "eventName": "intervention",
        "eventType": "intervention",
        "eventDate": epoch_ms(2026, 6, 30),
        "eventStart": epoch_ms(2026, 6, 30, 11, 34),
        "eventTotalDurationPDF": epoch_ms(2026, 6, 30, 13, 31),
        "eventPlanDur": 7200000,
        "eventDoneDur": 7200000,
        "eventStatus": "evt_status_done",
        "houseServiceId": 730187147,
        "eventPostponed": False,
        "hsType": "menage_repassage",
        "hsLevel": "M2",
        "idNewVisit": "00000000-0000-0000-0000-000000000000",
        "isVisitModificationExternal": True,
        "customerId": "123456789",
        "lastName": "DUPONT",
        "firstName": "Camille",
        "civility": "mme",
        "hsCliSerialNumber": "O2C00000000XX",
        "hsCity": "Lyon 1er Arrondissement",
    }
    event.update(overrides)
    return event


class NormalisationTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()

    def test_maps_a_nominal_event(self):
        service = normalize_event(make_event())

        self.assertEqual(service.event_id, "730187166")
        self.assertEqual(service.service_type, "menage_repassage")
        self.assertEqual(service.service_type_label, "Ménage - repassage")
        self.assertEqual(service.category, "menage")
        self.assertEqual(service.status_label, "Réalisée")
        self.assertFalse(service.is_cancelled)
        self.assertFalse(service.is_mandataire)
        self.assertEqual(service.duration_planned_min, 120)
        self.assertEqual(service.duration_done_min, 120)
        self.assertEqual(service.city, "Lyon 1er Arrondissement")
        self.assertEqual(service.worker.display_name, "Mme Camille DUPONT")
        self.assertEqual(service.contract_ref, "O2C00000000XX")

    def test_times_are_paris_local_and_end_uses_planned_duration(self):
        service = normalize_event(make_event())

        self.assertEqual(service.start.hour, 11)
        self.assertEqual(service.start.minute, 34)
        self.assertEqual(service.start.utcoffset(), timedelta(hours=2))  # CEST
        self.assertEqual(service.end - service.start, timedelta(hours=2))
        # eventTotalDurationPDF is the real clock-out, distinct from the planned end.
        self.assertEqual(service.actual_end.hour, 13)
        self.assertEqual(service.actual_end.minute, 31)

    def test_winter_dates_use_the_right_offset(self):
        service = normalize_event(
            make_event(eventStart=epoch_ms(2026, 1, 13, 9, 0), eventDate=epoch_ms(2026, 1, 13))
        )
        self.assertEqual(service.start.utcoffset(), timedelta(hours=1))  # CET
        self.assertEqual(service.start.hour, 9)

    def test_server_labels_win_over_the_offline_table(self):
        service = normalize_event(
            make_event(),
            type_labels={"menage_repassage": "ménage - repassage"},
            level_labels={"M2": "M2 (Ménage Confort)"},
        )
        self.assertEqual(service.service_type_label, "ménage - repassage")
        self.assertEqual(service.level_label, "M2 (Ménage Confort)")

    def test_level_falls_back_to_its_code(self):
        self.assertEqual(normalize_event(make_event()).level_label, "M2")

    def test_cancelled_statuses(self):
        for code, label in [
            ("annul_cli_ok", "Annulée dans les délais"),
            ("annul_cli_HD", "Annulée hors délais"),
            ("annul_vac_scol", "Annulée pour cause de vacances scolaires"),
            ("annul_empl", "Annulée par l'intervenant"),
        ]:
            with self.subTest(code=code):
                service = normalize_event(make_event(eventStatus=code))
                self.assertTrue(service.is_cancelled)
                self.assertEqual(service.status_label, label)
                self.assertTrue(service.summary.startswith("[ANNULÉ]"))
                self.assertEqual(service.to_dict()["colour"], CANCELLED_COLOUR)

    def test_unknown_status_mirrors_the_website_fallback(self):
        service = normalize_event(make_event(eventStatus="evt_status_brand_new"))
        self.assertEqual(service.status_label, UNKNOWN_STATUS_LABEL)
        self.assertTrue(service.is_cancelled)

    def test_unknown_service_type_keeps_its_raw_code(self):
        service = normalize_event(make_event(hsType="hstype_something_new"))
        self.assertEqual(service.service_type_label, "hstype_something_new")
        self.assertEqual(service.category, "autre")

    def test_mandataire_flag_comes_from_the_type_suffix(self):
        self.assertTrue(normalize_event(make_event(hsType="seniors_fe_mand")).is_mandataire)

    def test_cancelled_event_without_done_duration(self):
        event = make_event(eventStatus="annul_cli_ok", eventPostponed=True)
        del event["eventDoneDur"]  # absent in real payloads for cancelled services
        service = normalize_event(event)
        self.assertIsNone(service.duration_done_min)
        self.assertTrue(service.is_postponed)

    def test_missing_planned_duration_gets_a_one_hour_fallback(self):
        service = normalize_event(make_event(eventPlanDur=0))
        self.assertEqual(service.end - service.start, timedelta(hours=1))

    def test_numeric_strings_are_accepted(self):
        service = normalize_event(make_event(eventPlanDur="7200000", eventId="42"))
        self.assertEqual(service.event_id, "42")
        self.assertEqual(service.duration_planned_min, 120)

    def test_unusable_events_are_skipped_not_fatal(self):
        self.assertIsNone(normalize_event({"eventId": 1}))  # no start
        self.assertIsNone(normalize_event({"eventStart": epoch_ms(2026, 6, 30)}))  # no id
        self.assertIsNone(normalize_event(make_event(eventStart="not-a-timestamp")))
        self.assertIsNone(normalize_event("garbage"))


class ContentHashTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()

    def test_stable_across_identical_payloads(self):
        self.assertEqual(
            normalize_event(make_event()).content_hash(),
            normalize_event(make_event()).content_hash(),
        )

    def test_changes_when_the_service_is_rescheduled_or_cancelled(self):
        base = normalize_event(make_event()).content_hash()
        moved = normalize_event(make_event(eventStart=epoch_ms(2026, 6, 30, 14, 0))).content_hash()
        cancelled = normalize_event(make_event(eventStatus="annul_cli_ok")).content_hash()
        reassigned = normalize_event(make_event(firstName="Alex")).content_hash()

        self.assertNotEqual(base, moved)
        self.assertNotEqual(base, cancelled)
        self.assertNotEqual(base, reassigned)

    def test_ignores_labels_so_it_does_not_flap_with_the_network(self):
        without = normalize_event(make_event()).content_hash()
        with_labels = normalize_event(
            make_event(), type_labels={"menage_repassage": "Autre libellé"}
        ).content_hash()
        self.assertEqual(without, with_labels)


class CollectionTest(unittest.TestCase):
    def setUp(self):
        reset_warnings()

    def test_sorts_by_start_and_deduplicates_by_id(self):
        events = [
            make_event(eventId=2, eventStart=epoch_ms(2026, 7, 7, 9, 0)),
            make_event(eventId=1, eventStart=epoch_ms(2026, 6, 30, 9, 0)),
            make_event(eventId=2, eventStart=epoch_ms(2026, 7, 7, 9, 0)),
            {"broken": True},
        ]
        services = normalize_all(events)
        self.assertEqual([s.event_id for s in services], ["1", "2"])

    def test_handles_an_empty_planning(self):
        self.assertEqual(normalize_all([]), [])
        self.assertEqual(normalize_all(None), [])

    def test_document_envelope(self):
        start = datetime(2026, 6, 1, tzinfo=PARIS)
        end = datetime(2026, 9, 1, tzinfo=PARIS)
        document = to_document(normalize_all([make_event()]), start, end)

        self.assertEqual(document["count"], 1)
        self.assertEqual(document["source"], "client.o2.fr")
        self.assertEqual(document["window"]["start"], start.isoformat())
        self.assertIn("content_hash", document["services"][0])
        self.assertNotIn("raw", document["services"][0])

    def test_raw_payload_is_opt_in(self):
        document = to_document(
            normalize_all([make_event()]),
            datetime(2026, 6, 1, tzinfo=PARIS),
            datetime(2026, 9, 1, tzinfo=PARIS),
            include_raw=True,
        )
        self.assertEqual(document["services"][0]["raw"]["hsType"], "menage_repassage")


if __name__ == "__main__":
    unittest.main()
