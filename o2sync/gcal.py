"""Google Calendar access, built on the official ``google-api-python-client``.

Everything that talks to Google lives here; the decisions about *what* to change
live in :mod:`o2sync.sync`, which has no network dependency and is unit-tested.

The calendar is its own state store. Every event we create carries an
``extendedProperties.private`` block with the O2 event id and a content hash, and
every read filters on ``o2Source``. There is no local state file to lose, and we
can never enumerate — let alone delete — an event we did not create.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .errors import CalendarAPIError
from .model import Service

log = logging.getLogger(__name__)

CALENDAR_TIMEZONE = "Europe/Paris"
DEFAULT_CALENDAR_NAME = "O2 – Prestations"
CALENDAR_DESCRIPTION = (
    "Prestations O2 synchronisées automatiquement depuis client.o2.fr. "
    "Les modifications faites ici seront écrasées à la prochaine synchronisation."
)

#: Marker written to (and filtered on) every event we own.
SOURCE = "client.o2.fr"
PROP_SOURCE = "o2Source"
PROP_EVENT_ID = "o2EventId"
PROP_HASH = "o2ContentHash"
PROP_STATUS = "o2Status"

#: Retries inside the Google client for transient 5xx and rate limiting.
NUM_RETRIES = 3

#: Named in error messages so the scope/permission trade-off is obvious when it bites.
SCOPE_HINT = "calendar.app.created"

#: Service category -> Google colour id, chosen to approximate the extranet's own
#: palette. Google only offers 11 fixed event colours, so these are the nearest fit.
CATEGORY_COLOURS = {
    "menage": "6",  # Tangerine, for O2's orange
    "garde_enfants": "4",  # Flamingo, for pink
    "seniors": "3",  # Grape, for purple
    "jardinage": "10",  # Basil, for green
    "soutien_scolaire": "5",  # Banana
    "handicap": "7",  # Peacock
    "incapacite": "7",  # Peacock
    "o2_pro_menage": "9",  # Blueberry
}
CANCELLED_COLOUR_ID = "8"  # Graphite


def event_id_for(o2_event_id: str) -> str:
    """Deterministic Google event id for an O2 event.

    Google restricts event ids to base32hex — ``[a-v0-9]{5,1024}`` — so a hex
    digest prefixed with ``o2`` is always valid, and the same O2 service always
    maps to the same calendar event even if our local state is wiped.
    """
    digest = hashlib.sha1(f"o2:{o2_event_id}".encode()).hexdigest()
    return f"o2{digest}"


def _description(service: Service) -> str:
    lines = [f"Statut : {service.status_label}"]
    if service.is_postponed:
        lines.append("Prestation reprogrammée")
    if service.level_label:
        lines.append(f"Prestation : {service.level_label}")
    if service.worker.display_name:
        lines.append(f"Intervenant·e : {service.worker.display_name}")
    if service.duration_planned_min:
        lines.append(f"Durée prévue : {service.duration_planned_min} min")
    if service.duration_done_min:
        lines.append(f"Durée réalisée : {service.duration_done_min} min")
    if service.city:
        lines.append(f"Ville : {service.city}")
    if service.contract_ref:
        lines.append(f"Contrat : {service.contract_ref}")
    lines.append("")
    lines.append(f"Synchronisé depuis {SOURCE} (réf. {service.event_id}).")
    return "\n".join(lines)


def event_summary(service: Service) -> str:
    """``Ménage - repassage — Magda``, with ``[ANNULÉ] `` in front when cancelled."""
    title = service.service_type_label or service.service_type or "Prestation O2"
    if service.worker.first_name:
        title = f"{title} — {service.worker.first_name}"
    return f"[ANNULÉ] {title}" if service.is_cancelled else title


def build_event_body(service: Service) -> dict[str, Any]:
    """Render a :class:`Service` as a Google Calendar event resource."""
    body: dict[str, Any] = {
        "id": event_id_for(service.event_id),
        "summary": event_summary(service),
        "description": _description(service),
        "start": {"dateTime": service.start.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "end": {"dateTime": service.end.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "source": {"title": "Espace client O2", "url": "https://client.o2.fr/"},
        "extendedProperties": {
            "private": {
                PROP_SOURCE: SOURCE,
                PROP_EVENT_ID: service.event_id,
                PROP_HASH: service.content_hash(),
                PROP_STATUS: service.status,
            }
        },
    }
    if service.city:
        body["location"] = service.city

    if service.is_cancelled:
        body["colorId"] = CANCELLED_COLOUR_ID
        # A cancelled service must not make the user look busy, nor notify them.
        body["transparency"] = "transparent"
        body["reminders"] = {"useDefault": False, "overrides": []}
    else:
        colour = CATEGORY_COLOURS.get(service.category)
        if colour:
            body["colorId"] = colour
        body["transparency"] = "opaque"
    return body


def stored_hash(event: dict[str, Any]) -> str:
    return _private(event).get(PROP_HASH, "")


def stored_event_id(event: dict[str, Any]) -> str:
    return _private(event).get(PROP_EVENT_ID, "")


def is_ours(event: dict[str, Any]) -> bool:
    return _private(event).get(PROP_SOURCE) == SOURCE


def _private(event: dict[str, Any]) -> dict[str, str]:
    props = (event or {}).get("extendedProperties") or {}
    private = props.get("private") or {}
    return private if isinstance(private, dict) else {}


class CalendarClient:
    """Thin wrapper over the Calendar v3 service.

    Kept small and boring on purpose: :mod:`o2sync.sync` is written against this
    interface, so the tests can swap in a fake and exercise every decision path
    without a network or credentials.
    """

    def __init__(self, credentials: Any = None, service: Any = None) -> None:
        if service is None:
            from googleapiclient.discovery import build

            # cache_discovery=False avoids the noisy oauth2client file cache warning.
            service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self.service = service

    # ------------------------------------------------------------------ calendar

    def create_calendar(self, name: str = DEFAULT_CALENDAR_NAME) -> str:
        """Create the dedicated calendar and return its id.

        ``calendars.insert`` is one of the few methods the ``calendar.app.created``
        scope allows, which is why bootstrapping works even though discovery does
        not.
        """
        try:
            created = (
                self.service.calendars()
                .insert(
                    body={
                        "summary": name,
                        "description": CALENDAR_DESCRIPTION,
                        "timeZone": CALENDAR_TIMEZONE,
                    }
                )
                .execute(num_retries=NUM_RETRIES)
            )
        except Exception as exc:
            raise CalendarAPIError(f"Could not create calendar {name!r}: {exc}") from exc

        log.info("Created calendar %r (%s)", name, created["id"])
        return created["id"]

    def find_calendar_by_name(self, name: str) -> str | None:
        """Look the calendar up by name, or ``None`` if we are not allowed to look.

        ``calendarList.list`` needs ``calendar.readonly`` / ``calendar`` /
        ``calendar.calendarlist*``; the ``calendar.app.created`` scope this tool
        requests is deliberately not one of them. So discovery is a bonus for
        anyone who granted a broader scope, never something to depend on — hence
        the 403 is swallowed rather than raised.
        """
        from googleapiclient.errors import HttpError

        try:
            page_token = None
            while True:
                response = (
                    self.service.calendarList()
                    .list(pageToken=page_token, showHidden=True)
                    .execute(num_retries=NUM_RETRIES)
                )
                for entry in response.get("items", []):
                    if entry.get("summary") == name:
                        log.info("Found calendar %r (%s)", name, entry["id"])
                        return entry["id"]
                page_token = response.get("nextPageToken")
                if not page_token:
                    return None
        except HttpError as exc:
            if getattr(exc, "status_code", None) == 403 or "403" in str(exc):
                log.debug("Not allowed to list calendars with the current scope.")
                return None
            raise CalendarAPIError(f"Could not list calendars: {exc}") from exc
        except Exception as exc:
            raise CalendarAPIError(f"Could not list calendars: {exc}") from exc

    def resolve_calendar(
        self, name: str = DEFAULT_CALENDAR_NAME, calendar_id: str | None = None
    ) -> str:
        """Return the id of the calendar to sync into.

        An explicit id always wins. Otherwise we try discovery, which only works
        with a broader scope; failing that the caller has to bootstrap once with
        ``o2sync calendar-init``. We deliberately do **not** create a calendar as a
        fallback here: with no way to find it again, every run would make another
        one.
        """
        if calendar_id:
            return calendar_id

        found = self.find_calendar_by_name(name)
        if found:
            return found

        raise CalendarAPIError(
            f"No calendar id configured, and this app's scope ({SCOPE_HINT}) cannot list "
            "calendars to find one. Create it once with 'python -m o2sync calendar-init', "
            "then set the O2_CALENDAR_ID it prints."
        )

    # -------------------------------------------------------------------- events

    def list_synced_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Return the events *we* created inside the window.

        The ``privateExtendedProperty`` filter is what keeps this tool from ever
        seeing, updating or deleting an event it did not create.
        """
        events: list[dict[str, Any]] = []
        page_token = None
        try:
            while True:
                response = (
                    self.service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=start.isoformat(),
                        timeMax=end.isoformat(),
                        privateExtendedProperty=f"{PROP_SOURCE}={SOURCE}",
                        singleEvents=True,
                        showDeleted=False,
                        maxResults=2500,
                        pageToken=page_token,
                    )
                    .execute(num_retries=NUM_RETRIES)
                )
                events.extend(response.get("items", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception as exc:
            raise CalendarAPIError(f"Could not list calendar events: {exc}") from exc
        return events

    def create_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Insert an event, falling back to update when the id is already taken.

        Deleting a Google Calendar event leaves a tombstone that keeps its id
        reserved, so a service that was removed from O2 and later came back would
        fail to insert forever. Updating revives the tombstone instead.
        """
        from googleapiclient.errors import HttpError

        try:
            return (
                self.service.events()
                .insert(calendarId=calendar_id, body=body)
                .execute(num_retries=NUM_RETRIES)
            )
        except HttpError as exc:
            if getattr(exc, "status_code", None) == 409 or "409" in str(exc):
                log.debug("Event id %s already exists; updating instead.", body.get("id"))
                return self.update_event(calendar_id, body["id"], body)
            raise CalendarAPIError(f"Could not create event {body.get('id')}: {exc}") from exc
        except Exception as exc:
            raise CalendarAPIError(f"Could not create event {body.get('id')}: {exc}") from exc

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return (
                self.service.events()
                .update(calendarId=calendar_id, eventId=event_id, body=body)
                .execute(num_retries=NUM_RETRIES)
            )
        except Exception as exc:
            raise CalendarAPIError(f"Could not update event {event_id}: {exc}") from exc

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event, tolerating one that is already gone."""
        from googleapiclient.errors import HttpError

        try:
            self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute(
                num_retries=NUM_RETRIES
            )
        except HttpError as exc:
            status = getattr(exc, "status_code", None)
            if status in (404, 410) or "404" in str(exc) or "410" in str(exc):
                log.debug("Event %s was already deleted.", event_id)
                return
            raise CalendarAPIError(f"Could not delete event {event_id}: {exc}") from exc
        except Exception as exc:
            raise CalendarAPIError(f"Could not delete event {event_id}: {exc}") from exc


def summarise(services: Iterable[Service]) -> str:
    services = list(services)
    cancelled = sum(1 for s in services if s.is_cancelled)
    return f"{len(services)} service(s), {cancelled} cancelled"
