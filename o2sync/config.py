"""Credential loading.

Kept deliberately small: a five-line ``.env`` reader beats adding a dependency,
and the precedence rules are the boring ones (explicit flag, then environment,
then ``.env``, then prompt).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_EMAIL = "O2_EMAIL"
ENV_PASSWORD = "O2_PASSWORD"


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Parse a ``KEY=value`` file. Missing file is not an error.

    Supports ``export`` prefixes, ``#`` comments and quoted values. Anything more
    exotic than that is out of scope on purpose.
    """
    file = Path(path)
    if not file.is_file():
        return {}

    values: dict[str, str] = {}
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Could not read %s: %s", file, exc)
        return {}

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_credentials(
    email: str | None = None,
    password: str | None = None,
    dotenv_path: str | Path = ".env",
    allow_prompt: bool = True,
) -> tuple[str, str]:
    """Return ``(email, password)`` or raise ``SystemExit`` with a usable message."""
    dotenv = load_dotenv(dotenv_path)

    email = email or os.environ.get(ENV_EMAIL) or dotenv.get(ENV_EMAIL)
    password = password or os.environ.get(ENV_PASSWORD) or dotenv.get(ENV_PASSWORD)

    if not email and allow_prompt:
        try:
            email = input("O2 e-mail: ").strip()
        except (EOFError, KeyboardInterrupt):
            email = None
    if not password and allow_prompt:
        from getpass import getpass

        try:
            password = getpass("O2 password: ")
        except (EOFError, KeyboardInterrupt):
            password = None

    if not email or not password:
        raise SystemExit(
            "Missing credentials. Set %s and %s in the environment or a .env file "
            "(see .env.example)." % (ENV_EMAIL, ENV_PASSWORD)
        )
    return email, password
