"""Deciding what to change in the calendar.

This module is pure: it takes the services fetched from O2 plus the events already
on the calendar and returns a plan. No network, no credentials, no clock — which
is what makes the risky part (deletion) straightforward to test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from . import gcal
from .model import PARIS, Service

log = logging.getLogger(__name__)

CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
DELETE = "delete"


@dataclass
class Action:
    """One pending change. ``service`` is ``None`` for deletions."""

    kind: str
    calendar_event_id: str
    label: str
    service: Service | None = None
    body: dict[str, Any] | None = None
    reason: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.reason})" if self.reason else ""
        return f"{self.kind.upper():<9} {self.label}{suffix}"


@dataclass
class SyncPlan:
    create: list[Action] = field(default_factory=list)
    update: list[Action] = field(default_factory=list)
    unchanged: list[Action] = field(default_factory=list)
    delete: list[Action] = field(default_factory=list)

    @property
    def writes(self) -> list[Action]:
        return self.create + self.update + self.delete

    @property
    def is_empty(self) -> bool:
        return not self.writes

    def summary(self) -> str:
        return (
            f"{len(self.create)} created, {len(self.update)} updated, "
            f"{len(self.unchanged)} unchanged, {len(self.delete)} deleted"
        )


def _event_start(event: dict[str, Any]) -> datetime | None:
    """Parse an event's start into an aware datetime, or ``None`` if unparseable."""
    start = (event or {}).get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=PARIS)


def _label(service: Service) -> str:
    # Uses the calendar title, not the model's, so a --dry-run preview reads
    # exactly like the event that will land in the calendar.
    return f"{service.start:%Y-%m-%d %H:%M} {gcal.event_summary(service)}"


def plan(
    services: Iterable[Service],
    existing_events: Iterable[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> SyncPlan:
    """Work out what to create, update, leave alone, and delete.

    ``existing_events`` should be the events this tool created inside the window
    (see :meth:`gcal.CalendarClient.list_synced_events`); anything without our
    marker is ignored defensively even if it slips through.
    """
    services = list(services)
    result = SyncPlan()

    by_o2_id: dict[str, dict[str, Any]] = {}
    for event in existing_events:
        if not gcal.is_ours(event):
            # Should be impossible given the API-side filter; belt and braces,
            # because the alternative is deleting somebody else's event.
            log.debug("Ignoring calendar event without our marker: %s", event.get("id"))
            continue
        o2_id = gcal.stored_event_id(event)
        if not o2_id:
            continue
        if o2_id in by_o2_id:
            # Two calendar events claim the same O2 service. Keep the first and
            # remove the duplicate rather than fighting over it every run.
            result.delete.append(
                Action(
                    DELETE,
                    event.get("id", ""),
                    event.get("summary", o2_id),
                    reason="duplicate of another synced event",
                )
            )
            continue
        by_o2_id[o2_id] = event

    seen: set[str] = set()
    for service in services:
        seen.add(service.event_id)
        body = gcal.build_event_body(service)
        existing = by_o2_id.get(service.event_id)

        if existing is None:
            result.create.append(
                Action(CREATE, body["id"], _label(service), service=service, body=body)
            )
            continue

        current_hash = gcal.stored_hash(existing)
        if current_hash == service.content_hash():
            result.unchanged.append(
                Action(UNCHANGED, existing.get("id", body["id"]), _label(service), service=service)
            )
            continue

        result.update.append(
            Action(
                UPDATE,
                existing.get("id", body["id"]),
                _label(service),
                service=service,
                body=body,
                reason="content changed" if current_hash else "no stored hash",
            )
        )

    for o2_id, event in by_o2_id.items():
        if o2_id in seen:
            continue
        start = _event_start(event)
        if start is None:
            log.warning(
                "Synced event %s has no readable start date; leaving it alone.", event.get("id")
            )
            continue
        # Only ever delete inside the window we actually queried. Without this a
        # run with a narrow --start/--end would wipe every service outside it.
        if not (window_start <= start <= window_end):
            continue
        result.delete.append(
            Action(
                DELETE,
                event.get("id", ""),
                event.get("summary", o2_id),
                reason="no longer in the O2 planning",
            )
        )

    return result


def apply(
    sync_plan: SyncPlan,
    client: "gcal.CalendarClient",
    calendar_id: str,
    dry_run: bool = False,
) -> SyncPlan:
    """Execute a plan. With ``dry_run`` nothing is written."""
    if dry_run:
        for action in sync_plan.writes:
            log.info("[dry-run] %s", action)
        return sync_plan

    for action in sync_plan.create:
        log.debug("Creating %s", action.label)
        client.create_event(calendar_id, action.body)
    for action in sync_plan.update:
        log.debug("Updating %s", action.label)
        client.update_event(calendar_id, action.calendar_event_id, action.body)
    for action in sync_plan.delete:
        log.debug("Deleting %s", action.label)
        client.delete_event(calendar_id, action.calendar_event_id)
    return sync_plan
