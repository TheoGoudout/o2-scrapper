"""Normalisation of O2's raw planning payload into stable :class:`Service` records.

The raw payload is a flat JSON object per event with French domain shorthands and
epoch-millisecond timestamps. Everything here is defensive on purpose: a single
malformed event must never take a whole run down, and an unknown status or
service type must degrade to something readable rather than raise.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

#: O2 is a French company and the extranet renders every time in local Paris time.
#: Verified against the live API: ``eventDate`` lands exactly on Paris midnight.
PARIS = ZoneInfo("Europe/Paris")

#: What the site itself falls back to for a status it does not recognise.
UNKNOWN_STATUS_LABEL = "Non réalisable"

#: status code -> (human label, counts as cancelled)
#:
#: The three ``evt_status_*`` and three ``annul_*`` codes come from the site's own
#: JavaScript; ``annul_empl`` was found in live data and is *not* handled by the
#: site, which silently renders it as "Non réalisable".
STATUSES: dict[str, tuple[str, bool]] = {
    "evt_status_planned": ("Planifiée", False),
    "evt_status_in_progress": ("En cours", False),
    "evt_status_done": ("Réalisée", False),
    "annul_vac_scol": ("Annulée pour cause de vacances scolaires", True),
    "annul_cli_ok": ("Annulée dans les délais", True),
    "annul_cli_HD": ("Annulée hors délais", True),
    "annul_empl": ("Annulée par l'intervenant", True),
}

#: service code -> (category, colour, offline label)
#:
#: Categories and colours are lifted from the extranet's own styling so a future
#: calendar sink can colour events the same way the website does. The label is
#: only a fallback: :meth:`O2Client.hs_type_label` asks the server for the real
#: one. ``None`` means "we have no verified translation, use whatever the server
#: says, or the raw code".
HS_TYPES: dict[str, tuple[str, str, str | None]] = {
    "menage": ("menage", "#EA5602", "Ménage"),
    "menage_repassage": ("menage", "#EA5602", "Ménage - repassage"),
    "repassage": ("menage", "#EA5602", "Repassage"),
    "garde_enfant": ("garde_enfants", "#DE1C85", "Garde d'enfants"),
    "ge_inf_trois_ans": ("garde_enfants", "#DE1C85", "Garde d'enfants de moins de 3 ans"),
    "jardinage": ("jardinage", "#009A49", "Jardinage"),
    "soutien_scolaire": ("soutien_scolaire", "#f4a73a", "Soutien scolaire"),
    "o2_pro_menage": ("o2_pro_menage", "#0e7891", "O2 Pro - ménage"),
    "seniors_fe_mand": ("seniors", "#702785", None),
    "hstype_seniors_ap": ("seniors", "#702785", None),
    "hstype_seniors_fe": ("seniors", "#702785", None),
    "hstype_seniors_mad": ("seniors", "#702785", None),
    "hstype_hdicap_adulte": ("handicap", "#0099BC", None),
    "hstype_handicap_ge": ("handicap", "#0099BC", None),
    "hstype_handicap_mad": ("handicap", "#0099BC", None),
    "hstype_incapacite": ("incapacite", "#00AED9", None),
}

DEFAULT_COLOUR = "#EA5602"
CANCELLED_COLOUR = "#999999"

# Unknown codes are reported once per run instead of once per event.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def reset_warnings() -> None:
    """Forget which unknown codes were already reported (used by the tests)."""
    _warned.clear()


def _as_int(value: Any) -> int | None:
    """Coerce to int, tolerating numeric strings and rejecting anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _from_epoch_ms(value: Any) -> datetime | None:
    ms = _as_int(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, PARIS)
    except (OverflowError, OSError, ValueError):
        return None


def status_info(code: str) -> tuple[str, bool]:
    """Return ``(label, is_cancelled)`` for a status code.

    Unknown codes mirror the website: labelled "Non réalisable" and treated as not
    happening. That is deliberately conservative — if O2 ever adds a new *active*
    status we would grey it out rather than silently show a service that is off.
    The warning is there to make that visible.
    """
    if code in STATUSES:
        return STATUSES[code]
    _warn_once(
        "status:" + code,
        "Unknown event status %r; treating it as %r like the website does.",
        code,
        UNKNOWN_STATUS_LABEL,
    )
    return UNKNOWN_STATUS_LABEL, True


def type_info(code: str) -> tuple[str, str, str | None]:
    """Return ``(category, colour, offline label)`` for a service type code."""
    if code in HS_TYPES:
        return HS_TYPES[code]
    _warn_once("hstype:" + code, "Unknown service type %r; falling back to its raw code.", code)
    return "autre", DEFAULT_COLOUR, None


@dataclass(frozen=True)
class Worker:
    """The person carrying out the service ("l'intervenant")."""

    civility: str = ""
    first_name: str = ""
    last_name: str = ""

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.civility.capitalize(), self.first_name, self.last_name) if p]
        return " ".join(parts)


@dataclass(frozen=True)
class Service:
    """One planned (or cancelled, or completed) O2 service.

    ``event_id`` is stable across runs even when the service is rescheduled, which
    is what lets a calendar sink update an existing event instead of recreating it.
    """

    event_id: str
    start: datetime
    end: datetime
    duration_planned_min: int | None
    duration_done_min: int | None
    actual_end: datetime | None

    status: str
    status_label: str
    is_cancelled: bool
    is_postponed: bool

    service_type: str
    service_type_label: str
    category: str
    colour: str
    is_mandataire: bool

    level: str
    level_label: str

    city: str
    worker: Worker

    house_service_id: str
    customer_id: str
    contract_ref: str
    series_id: str

    raw: Mapping[str, Any] = field(default=None, repr=False, compare=False)  # type: ignore[assignment]

    @property
    def summary(self) -> str:
        """Title suitable for a calendar event."""
        title = self.service_type_label or self.service_type or "Prestation O2"
        return f"[ANNULÉ] {title}" if self.is_cancelled else title

    def content_hash(self) -> str:
        """Fingerprint of everything a calendar event would show.

        Derived labels are excluded on purpose: they are fetched from the network
        and could flap independently of the underlying data, which would cause
        pointless calendar updates.
        """
        payload = [
            self.event_id,
            self.start.isoformat(),
            self.end.isoformat(),
            self.status,
            str(self.duration_planned_min),
            str(self.duration_done_min),
            str(self.is_postponed),
            self.service_type,
            self.level,
            self.city,
            self.worker.display_name,
            self.house_service_id,
        ]
        return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "actual_end": self.actual_end.isoformat() if self.actual_end else None,
            "duration_planned_min": self.duration_planned_min,
            "duration_done_min": self.duration_done_min,
            "status": self.status,
            "status_label": self.status_label,
            "is_cancelled": self.is_cancelled,
            "is_postponed": self.is_postponed,
            "service_type": self.service_type,
            "service_type_label": self.service_type_label,
            "category": self.category,
            "colour": CANCELLED_COLOUR if self.is_cancelled else self.colour,
            "is_mandataire": self.is_mandataire,
            "level": self.level,
            "level_label": self.level_label,
            "city": self.city,
            "worker": {
                "civility": self.worker.civility,
                "first_name": self.worker.first_name,
                "last_name": self.worker.last_name,
                "display_name": self.worker.display_name,
            },
            "house_service_id": self.house_service_id,
            "customer_id": self.customer_id,
            "contract_ref": self.contract_ref,
            "series_id": self.series_id,
            "summary": self.summary,
            "content_hash": self.content_hash(),
        }
        if include_raw:
            data["raw"] = self.raw
        return data


def normalize_event(
    raw: Mapping[str, Any],
    type_labels: Mapping[str, str] | None = None,
    level_labels: Mapping[str, str] | None = None,
) -> Service | None:
    """Turn one raw API event into a :class:`Service`, or ``None`` if unusable.

    ``type_labels``/``level_labels`` are the server-provided translations; when a
    code is missing from them we fall back to the offline table, then to the code.
    """
    if not isinstance(raw, Mapping):
        _warn_once("shape", "Skipping a planning entry that is not an object: %r", type(raw))
        return None

    event_id = _as_text(raw.get("eventId"))
    start = _from_epoch_ms(raw.get("eventStart"))
    if not event_id or start is None:
        log.warning("Skipping event without a usable id/start date: %r", dict(raw))
        return None

    plan_ms = _as_int(raw.get("eventPlanDur"))
    done_ms = _as_int(raw.get("eventDoneDur"))  # absent on cancelled services
    end = start + timedelta(milliseconds=plan_ms if plan_ms and plan_ms > 0 else 0)
    if end <= start:
        # A zero/absent planned duration would produce a zero-length calendar entry.
        end = start + timedelta(hours=1)
        _warn_once(
            "duration",
            "Event %s has no usable planned duration; assuming 1 hour.",
            event_id,
        )

    status = _as_text(raw.get("eventStatus"))
    status_label, is_cancelled = status_info(status)

    hs_type = _as_text(raw.get("hsType"))
    category, colour, offline_label = type_info(hs_type)
    type_label = (type_labels or {}).get(hs_type) or offline_label or hs_type

    level = _as_text(raw.get("hsLevel"))
    level_label = (level_labels or {}).get(level) or level

    return Service(
        event_id=event_id,
        start=start,
        end=end,
        duration_planned_min=plan_ms // 60000 if plan_ms else None,
        duration_done_min=done_ms // 60000 if done_ms else None,
        actual_end=_from_epoch_ms(raw.get("eventTotalDurationPDF")),
        status=status,
        status_label=status_label,
        is_cancelled=is_cancelled,
        is_postponed=bool(raw.get("eventPostponed")),
        service_type=hs_type,
        service_type_label=type_label,
        category=category,
        colour=colour,
        # The extranet flags "mandataire" contracts by a suffix on the type code.
        is_mandataire="_mand" in hs_type,
        level=level,
        level_label=level_label,
        city=_as_text(raw.get("hsCity")),
        worker=Worker(
            civility=_as_text(raw.get("civility")),
            first_name=_as_text(raw.get("firstName")),
            last_name=_as_text(raw.get("lastName")),
        ),
        house_service_id=_as_text(raw.get("houseServiceId")),
        customer_id=_as_text(raw.get("customerId")),
        contract_ref=_as_text(raw.get("hsCliSerialNumber")),
        series_id=_as_text(raw.get("idNewVisit")),
        raw=dict(raw),
    )


def normalize_all(
    events: Iterable[Any],
    type_labels: Mapping[str, str] | None = None,
    level_labels: Mapping[str, str] | None = None,
) -> list[Service]:
    """Normalise every event, dropping unusable ones and de-duplicating by id."""
    services: dict[str, Service] = {}
    for raw in events or ():
        service = normalize_event(raw, type_labels, level_labels)
        if service is not None:
            services[service.event_id] = service
    return sorted(services.values(), key=lambda s: (s.start, s.event_id))


def to_document(
    services: list[Service],
    window_start: datetime,
    window_end: datetime,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Wrap normalised services in the top-level JSON document the CLI writes."""
    return {
        "generated_at": datetime.now(PARIS).isoformat(),
        "source": "client.o2.fr",
        "timezone": str(PARIS),
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "count": len(services),
        "services": [s.to_dict(include_raw=include_raw) for s in services],
    }


def dumps(document: Mapping[str, Any], pretty: bool = False) -> str:
    """Serialise a document, keeping accents readable rather than escaped."""
    if pretty:
        return json.dumps(document, ensure_ascii=False, indent=2)
    return json.dumps(document, ensure_ascii=False)
