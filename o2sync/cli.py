"""Command line entry point: log in to O2 and write the planning as JSON."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .client import O2Client
from .config import resolve_credentials
from .errors import AuthError, O2Error, ServiceUnavailable, TransportError
from .model import PARIS, dumps, normalize_all, to_document

log = logging.getLogger("o2sync")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2
EXIT_UNAVAILABLE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="o2sync",
        description="Fetch your O2 (client.o2.fr) planned services and emit them as JSON.",
        epilog="Credentials come from --email/--password, the O2_EMAIL/O2_PASSWORD "
        "environment variables, a .env file, or an interactive prompt.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    window = parser.add_argument_group("window")
    window.add_argument(
        "--days-back", type=int, default=30, help="how far back to look (default: 30)"
    )
    window.add_argument(
        "--days-forward", type=int, default=90, help="how far ahead to look (default: 90)"
    )
    window.add_argument("--start", help="explicit start date (YYYY-MM-DD), overrides --days-back")
    window.add_argument("--end", help="explicit end date (YYYY-MM-DD), overrides --days-forward")
    window.add_argument(
        "--chunk-days",
        type=int,
        default=0,
        help="split the window into N-day requests and merge the results "
        "(default: 0, a single request, which is enough in practice)",
    )

    creds = parser.add_argument_group("credentials")
    creds.add_argument("--email", help="O2 account e-mail")
    creds.add_argument(
        "--password",
        help="O2 account password (prefer O2_PASSWORD or .env: argv is visible to other processes)",
    )
    creds.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    creds.add_argument(
        "--no-prompt", action="store_true", help="fail instead of asking for missing credentials"
    )

    output = parser.add_argument_group("output")
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

    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="log each step")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only log errors")
    return parser


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
        else (now - timedelta(days=args.days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
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


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)

    start, end = resolve_window(args)
    email, password = resolve_credentials(
        args.email, args.password, dotenv_path=args.env_file, allow_prompt=not args.no_prompt
    )

    log.info("Fetching O2 planning from %s to %s", start.date(), end.date())

    with O2Client() as client:
        client.login(email, password)
        raw_events = client.fetch_events_chunked(start, end, args.chunk_days)
        log.info("O2 returned %d event(s)", len(raw_events))

        if args.dump_raw:
            _write(args.dump_raw, json.dumps(raw_events, ensure_ascii=False, indent=2))

        if args.no_labels:
            type_labels: dict[str, str] = {}
            level_labels: dict[str, str] = {}
        else:
            type_labels, level_labels = client.resolve_labels(raw_events)

    services = normalize_all(raw_events, type_labels, level_labels)
    if len(services) != len(raw_events):
        log.warning("Kept %d of %d event(s) after normalisation", len(services), len(raw_events))

    cancelled = sum(1 for s in services if s.is_cancelled)
    log.info("%d service(s), of which %d cancelled", len(services), cancelled)

    document = to_document(services, start, end, include_raw=args.include_raw)
    _write(args.output, dumps(document, pretty=args.pretty))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return EXIT_ERROR
    except AuthError as exc:
        log.error("Authentication failed: %s", exc)
        return EXIT_AUTH
    except (ServiceUnavailable, TransportError) as exc:
        log.error("O2 is unreachable or misbehaving: %s", exc)
        return EXIT_UNAVAILABLE
    except O2Error as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
