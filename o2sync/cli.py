"""Command line entry point.

Three subcommands: ``fetch`` (O2 planning to JSON), ``sync`` (O2 planning to a
dedicated Google Calendar) and ``auth`` (the one-time Google consent flow).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .client import O2Client
from .config import resolve_credentials
from .errors import (
    AuthError,
    CalendarAPIError,
    GoogleAuthRequired,
    O2Error,
    ServiceUnavailable,
    TransportError,
)
from .model import PARIS, Service, dumps, normalize_all, to_document

log = logging.getLogger("o2sync")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2
EXIT_UNAVAILABLE = 3


# --------------------------------------------------------------------- arguments


def _add_window_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("window")
    group.add_argument("--days-back", type=int, default=30, help="how far back to look (default: 30)")
    group.add_argument(
        "--days-forward", type=int, default=90, help="how far ahead to look (default: 90)"
    )
    group.add_argument("--start", help="explicit start date (YYYY-MM-DD), overrides --days-back")
    group.add_argument("--end", help="explicit end date (YYYY-MM-DD), overrides --days-forward")
    group.add_argument(
        "--chunk-days",
        type=int,
        default=0,
        help="split the window into N-day requests and merge the results "
        "(default: 0, a single request, which is enough in practice)",
    )


def _add_o2_credential_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("O2 credentials")
    group.add_argument("--email", help="O2 account e-mail")
    group.add_argument(
        "--password",
        help="O2 account password (prefer O2_PASSWORD or .env: argv is visible to other processes)",
    )
    group.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    group.add_argument(
        "--no-prompt", action="store_true", help="fail instead of asking for missing credentials"
    )


def _add_google_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Google")
    group.add_argument(
        "--client-secrets",
        help="OAuth client secrets file (default: credentials.json, or O2_GOOGLE_CLIENT_SECRETS)",
    )
    group.add_argument(
        "--token", help="stored Google token (default: token.json, or O2_GOOGLE_TOKEN)"
    )


def _add_verbosity(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", "--verbose", action="store_true", help="log each step")
    group.add_argument("-q", "--quiet", action="store_true", help="only log errors")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="o2sync",
        description="Mirror your O2 (client.o2.fr) planned services into a Google Calendar.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch",
        help="fetch the O2 planning and emit it as JSON",
        description="Fetch your O2 planned services and emit them as JSON.",
    )
    _add_window_args(fetch)
    _add_o2_credential_args(fetch)
    output = fetch.add_argument_group("output")
    output.add_argument("-o", "--output", help="write JSON here instead of stdout")
    output.add_argument("--pretty", action="store_true", help="indent the JSON output")
    output.add_argument(
        "--include-raw", action="store_true", help="keep each event's untouched API payload"
    )
    output.add_argument("--dump-raw", help="also write the unmodified API response to this path")
    output.add_argument(
        "--no-labels",
        action="store_true",
        help="skip the label lookups and use the built-in French labels only",
    )
    _add_verbosity(fetch)
    fetch.set_defaults(handler=cmd_fetch)

    sync = subparsers.add_parser(
        "sync",
        help="synchronise the O2 planning into a dedicated Google Calendar",
        description="Create, update and remove Google Calendar events to match the O2 planning. "
        "Existing events are updated in place; cancelled services are kept and marked.",
    )
    _add_window_args(sync)
    _add_o2_credential_args(sync)
    _add_google_args(sync)
    calendar = sync.add_argument_group("calendar")
    calendar.add_argument(
        "--calendar-name",
        default=None,
        help="name of the dedicated calendar, created if missing (default: 'O2 – Prestations')",
    )
    calendar.add_argument(
        "--calendar-id", help="use this calendar id directly instead of looking one up by name"
    )
    calendar.add_argument(
        "--dry-run", action="store_true", help="show what would change without writing anything"
    )
    calendar.add_argument(
        "--from-json",
        help="plan from a previously fetched file instead of contacting O2 "
        "(expects the output of 'fetch --dump-raw', or of 'fetch --include-raw')",
    )
    calendar.add_argument(
        "--no-labels",
        action="store_true",
        help="skip the label lookups and use the built-in French labels only",
    )
    _add_verbosity(sync)
    sync.set_defaults(handler=cmd_sync)

    auth = subparsers.add_parser(
        "auth",
        help="run the one-time Google authorisation flow",
        description="Open a browser once to authorise access, and store the token locally. "
        "Only the 'calendar.app.created' scope is requested, so this tool can only ever "
        "touch calendars it created itself.",
    )
    _add_google_args(auth)
    auth.add_argument(
        "--print-env",
        action="store_true",
        help="print the stored credentials as environment variables, for deploying to a "
        "server that has no writable disk (secrets go to stdout: redirect with care)",
    )
    _add_verbosity(auth)
    auth.set_defaults(handler=cmd_auth)

    return parser


# ----------------------------------------------------------------------- helpers


def _parse_date(value: str, end_of_day: bool = False) -> datetime:
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"Invalid date {value!r}: expected YYYY-MM-DD") from None
    moment = day.replace(hour=23, minute=59, second=59) if end_of_day else day
    return moment.replace(tzinfo=PARIS)


def resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    now = datetime.now(PARIS)
    start = (
        _parse_date(args.start)
        if args.start
        else (now - timedelta(days=args.days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    )
    end = (
        _parse_date(args.end, end_of_day=True)
        if args.end
        else (now + timedelta(days=args.days_forward)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
    )
    if end <= start:
        raise SystemExit("The end of the window must be after its start.")
    return start, end


def _write(path: str | None, text: str) -> None:
    if not path:
        sys.stdout.write(text + "\n")
        return
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    log.info("Wrote %s", target)


def _fetch_services(args: argparse.Namespace, start: datetime, end: datetime) -> list[Service]:
    """Log into O2 and return the normalised services for the window."""
    email, password = resolve_credentials(
        args.email, args.password, dotenv_path=args.env_file, allow_prompt=not args.no_prompt
    )
    log.info("Fetching O2 planning from %s to %s", start.date(), end.date())

    with O2Client() as client:
        client.login(email, password)
        raw_events = client.fetch_events_chunked(start, end, args.chunk_days)
        log.info("O2 returned %d event(s)", len(raw_events))

        if getattr(args, "dump_raw", None):
            _write(args.dump_raw, json.dumps(raw_events, ensure_ascii=False, indent=2))

        if args.no_labels:
            type_labels: dict[str, str] = {}
            level_labels: dict[str, str] = {}
        else:
            type_labels, level_labels = client.resolve_labels(raw_events)

    services = normalize_all(raw_events, type_labels, level_labels)
    if len(services) != len(raw_events):
        log.warning("Kept %d of %d event(s) after normalisation", len(services), len(raw_events))
    return services


def _services_from_json(path: str) -> list[Service]:
    """Rebuild services from a previously fetched file.

    Only raw API payloads are accepted, so the normalisation always runs through
    the same code path as a live fetch rather than a second, divergent one.
    """
    try:
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from None
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from None

    if isinstance(data, list):
        return normalize_all(data)

    if isinstance(data, dict) and isinstance(data.get("services"), list):
        raws = [s.get("raw") for s in data["services"] if isinstance(s.get("raw"), dict)]
        if len(raws) == len(data["services"]):
            return normalize_all(raws)
        raise SystemExit(
            f"{path} has no raw payloads. Produce it with 'o2sync fetch --dump-raw {path}' "
            "or 'o2sync fetch --include-raw'."
        )

    raise SystemExit(f"{path} does not look like an O2 planning dump.")


# ---------------------------------------------------------------------- commands


def cmd_fetch(args: argparse.Namespace) -> int:
    start, end = resolve_window(args)
    services = _fetch_services(args, start, end)

    cancelled = sum(1 for s in services if s.is_cancelled)
    log.info("%d service(s), of which %d cancelled", len(services), cancelled)

    document = to_document(services, start, end, include_raw=args.include_raw)
    _write(args.output, dumps(document, pretty=args.pretty))
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    from . import gauth, gcal, sync as sync_module

    start, end = resolve_window(args)

    if args.from_json:
        services = _services_from_json(args.from_json)
        log.info("Loaded %d service(s) from %s", len(services), args.from_json)
    else:
        services = _fetch_services(args, start, end)

    credentials = gauth.load_credentials(args.client_secrets, args.token)
    client = gcal.CalendarClient(credentials)

    calendar_id = args.calendar_id or client.resolve_calendar(
        args.calendar_name or gcal.DEFAULT_CALENDAR_NAME
    )
    existing = client.list_synced_events(calendar_id, start, end)
    log.info("Calendar already holds %d synced event(s) in the window", len(existing))

    plan = sync_module.plan(services, existing, start, end)

    if args.dry_run:
        if plan.is_empty:
            log.info("Nothing to change.")
        for action in plan.writes:
            print(action)

    sync_module.apply(plan, client, calendar_id, dry_run=args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    log.info("%s%s", prefix, plan.summary())
    return EXIT_OK


def cmd_auth(args: argparse.Namespace) -> int:
    from . import gauth

    if args.print_env:
        # Reads the existing token rather than re-authorising, so this is safe to
        # re-run when you need the values again.
        print(gauth.env_block(args.token))
        return EXIT_OK

    token_path = gauth.run_consent_flow(args.client_secrets, args.token)
    log.info("Authorised. Token stored in %s", token_path)
    log.info("Next: python -m o2sync sync --dry-run")
    return EXIT_OK


# -------------------------------------------------------------------- entrypoint


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)

    return args.handler(args)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return EXIT_ERROR
    except GoogleAuthRequired as exc:
        log.error("Google authorisation needed: %s", exc)
        return EXIT_AUTH
    except AuthError as exc:
        log.error("Authentication failed: %s", exc)
        return EXIT_AUTH
    except CalendarAPIError as exc:
        log.error("Google Calendar request failed: %s", exc)
        return EXIT_UNAVAILABLE
    except (ServiceUnavailable, TransportError) as exc:
        log.error("O2 is unreachable or misbehaving: %s", exc)
        return EXIT_UNAVAILABLE
    except O2Error as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
