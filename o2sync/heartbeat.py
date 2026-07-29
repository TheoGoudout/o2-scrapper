"""Liveness heartbeat for long-running (``--interval``) syncs.

A scheduled container has no port to probe, so the loop touches a small JSON file
after every cycle and ``o2sync healthcheck`` reads it back. The file records the
interval it was written with, so the health check works out its own staleness
allowance instead of needing to be configured to match.

Health here means *the loop is alive*, not *the last sync succeeded*: restarting
the container cannot fix O2 or Google being down, so a transient failure is
logged loudly but still refreshes the heartbeat.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENV_PATH = "O2_HEARTBEAT_FILE"
DEFAULT_PATH = "/tmp/o2sync-heartbeat.json"

#: Slack on top of two missed cycles, to absorb a slow run or a clock wobble.
GRACE_SECONDS = 300


def resolve_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get(ENV_PATH) or DEFAULT_PATH)


def write(path: Path, interval: int, status: str, detail: str = "") -> None:
    """Record a completed cycle. Never raises: a failed heartbeat must not kill the loop."""
    payload = {
        "timestamp": time.time(),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "interval": interval,
        "status": status,
        "detail": detail[:500],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename, so a health check can never read a half-written file.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        log.warning("Could not write the heartbeat to %s: %s", path, exc)


def check(path: Path, now: float | None = None) -> tuple[bool, str]:
    """Return ``(healthy, message)`` for the heartbeat at ``path``."""
    now = time.time() if now is None else now

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"no heartbeat at {path} yet"
    except (OSError, ValueError) as exc:
        return False, f"unreadable heartbeat at {path}: {exc}"

    try:
        timestamp = float(payload["timestamp"])
        interval = int(payload["interval"])
    except (KeyError, TypeError, ValueError):
        return False, f"malformed heartbeat at {path}"

    age = now - timestamp
    allowance = interval * 2 + GRACE_SECONDS
    status = payload.get("status", "unknown")

    if age > allowance:
        return False, (
            f"heartbeat is {int(age)}s old, over the {allowance}s allowance "
            f"(interval {interval}s) — the sync loop looks stuck"
        )
    return True, f"alive, last cycle {int(age)}s ago ({status})"
