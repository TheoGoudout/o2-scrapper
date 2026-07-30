"""Google OAuth credentials for the calendar sync.

Consent and use are deliberately separated. ``sync`` must be safe to run from
cron, so it never opens a browser: if the stored token is missing or unusable it
raises :class:`GoogleAuthRequired` telling the user to run ``o2sync auth`` once.

The requested scope is ``calendar.app.created``, which only grants access to
calendars this application itself created. The tool therefore cannot read or
modify the user's primary calendar — or any other one — no matter what it does.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from .errors import GoogleAuthRequired

log = logging.getLogger(__name__)

#: Least privilege: secondary calendars created by this app, and nothing else.
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]

ENV_CLIENT_SECRETS = "O2_GOOGLE_CLIENT_SECRETS"
ENV_TOKEN = "O2_GOOGLE_TOKEN"

#: Stateless alternative to token.json, for containers and no-code platforms that
#: give you environment variables but no durable writable disk.
ENV_CLIENT_ID = "O2_GOOGLE_CLIENT_ID"
ENV_CLIENT_SECRET = "O2_GOOGLE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "O2_GOOGLE_REFRESH_TOKEN"

TOKEN_URI = "https://oauth2.googleapis.com/token"

DEFAULT_CLIENT_SECRETS = "credentials.json"
DEFAULT_TOKEN = "token.json"

SETUP_HINT = (
    "Create an OAuth client of type 'Desktop app' in Google Cloud, download it as "
    "%s, then run: python -m o2sync auth"
)


def resolve_paths(client_secrets: str | None = None, token: str | None = None) -> tuple[Path, Path]:
    """Resolve the two credential file paths from flags, then environment, then defaults."""
    secrets_path = client_secrets or os.environ.get(ENV_CLIENT_SECRETS) or DEFAULT_CLIENT_SECRETS
    token_path = token or os.environ.get(ENV_TOKEN) or DEFAULT_TOKEN
    return Path(secrets_path), Path(token_path)


def _save(credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    # Not every filesystem supports it, but where it does this is a long-lived secret.
    with contextlib.suppress(OSError):
        token_path.chmod(0o600)
    log.debug("Stored Google credentials in %s", token_path)


def _credentials_from_env(credentials_class):
    """Build credentials straight from environment variables, or ``None``.

    Google's refresh tokens for desktop clients are long-lived and are not rotated
    on use, so the three values below are all a stateless run ever needs.
    """
    client_id = os.environ.get(ENV_CLIENT_ID)
    client_secret = os.environ.get(ENV_CLIENT_SECRET)
    refresh_token = os.environ.get(ENV_REFRESH_TOKEN)

    if not refresh_token:
        return None
    if not (client_id and client_secret):
        raise GoogleAuthRequired(
            f"${ENV_REFRESH_TOKEN} is set but ${ENV_CLIENT_ID} and ${ENV_CLIENT_SECRET} are not. "
            "All three are needed for credential-free runs."
        )

    log.debug("Using Google credentials from the environment.")
    return credentials_class(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


def load_credentials(client_secrets: str | None = None, token: str | None = None):
    """Return usable Google credentials, refreshing them if needed.

    Never prompts. Raises :class:`GoogleAuthRequired` when a human has to run the
    consent flow.
    """
    from google.auth.exceptions import GoogleAuthError as _GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    secrets_path, token_path = resolve_paths(client_secrets, token)

    # A refresh token in the environment wins: it needs no writable disk, which is
    # what makes scheduled containers and no-code platforms workable.
    env_credentials = _credentials_from_env(Credentials)
    if env_credentials is not None:
        try:
            env_credentials.refresh(Request())
        except _GoogleAuthError as exc:
            raise GoogleAuthRequired(
                f"Could not refresh the Google token from ${ENV_REFRESH_TOKEN} ({exc}). "
                "Re-authorise with: python -m o2sync auth --print-env"
            ) from exc
        return env_credentials

    if not token_path.is_file():
        raise GoogleAuthRequired(
            f"No Google credentials at {token_path}. " + SETUP_HINT % secrets_path
        )

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, KeyError) as exc:
        raise GoogleAuthRequired(
            f"{token_path} is not a valid Google token file ({exc}). "
            "Delete it and run: python -m o2sync auth"
        ) from exc

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        log.debug("Google access token expired; refreshing.")
        try:
            credentials.refresh(Request())
        except _GoogleAuthError as exc:
            # Refresh tokens die when access is revoked or the password changes.
            raise GoogleAuthRequired(
                f"Could not refresh the Google token ({exc}). "
                "Re-authorise with: python -m o2sync auth"
            ) from exc
        _save(credentials, token_path)
        return credentials

    raise GoogleAuthRequired(
        f"The Google credentials in {token_path} are unusable and cannot be refreshed. "
        "Re-authorise with: python -m o2sync auth"
    )


def run_consent_flow(client_secrets: str | None = None, token: str | None = None) -> Path:
    """Run the one-time browser consent flow and store the token.

    Only ever called by the ``auth`` subcommand.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_path, token_path = resolve_paths(client_secrets, token)

    if not secrets_path.is_file():
        raise GoogleAuthRequired(
            f"No OAuth client secrets at {secrets_path}. " + SETUP_HINT % secrets_path
        )

    # google-auth-oauthlib happily accepts a "web" client here and only fails much
    # later, at Google's redirect_uri check, with an opaque error. Catch it now.
    try:
        config = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoogleAuthRequired(f"Could not read {secrets_path} as JSON: {exc}") from exc

    if isinstance(config, dict) and "installed" not in config:
        kind = ", ".join(config) or "nothing recognisable"
        raise GoogleAuthRequired(
            f"{secrets_path} contains {kind}, not a Desktop app client. In Google Cloud, under "
            "APIs & Services -> Google Auth Platform -> Clients, create a client with "
            "application type 'Desktop app' and download that JSON instead."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    except ValueError as exc:
        # By far the most common setup mistake: creating a "Web application"
        # client instead of a "Desktop app" one.
        raise GoogleAuthRequired(
            f"{secrets_path} is not a Desktop app OAuth client ({exc}). In Google Cloud, "
            "under APIs & Services -> Google Auth Platform -> Clients, create a client with "
            "application type 'Desktop app' and download that JSON instead."
        ) from exc
    except (OSError, KeyError) as exc:
        raise GoogleAuthRequired(f"Could not read {secrets_path}: {exc}") from exc

    import webbrowser

    try:
        # port=0 lets the OS pick a free port for the loopback redirect.
        # prompt=consent forces Google to hand back a refresh token every time,
        # even if this client was already authorised.
        credentials = flow.run_local_server(port=0, prompt="consent")
    except webbrowser.Error as exc:
        # Typically means this was run over SSH or in a container. The consent flow
        # needs a real browser on the same machine as the loopback listener.
        raise GoogleAuthRequired(
            f"No browser available to complete the consent flow ({exc}). Run "
            "'python -m o2sync auth' on your own computer, then copy the credentials to "
            "the server with 'python -m o2sync auth --print-env'."
        ) from exc

    if not credentials.refresh_token:
        raise GoogleAuthRequired(
            "Google did not return a refresh token, so unattended runs would stop working "
            "after an hour. Revoke this app's access at "
            "https://myaccount.google.com/permissions and run 'python -m o2sync auth' again."
        )

    _save(credentials, token_path)
    return token_path


def env_block(token: str | None = None) -> str:
    """Render the stored token as environment variables for a server deployment."""
    import json

    _, token_path = resolve_paths(None, token)
    if not token_path.is_file():
        raise GoogleAuthRequired(f"No token at {token_path}. Run: python -m o2sync auth")

    data = json.loads(token_path.read_text(encoding="utf-8"))
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
    if missing:
        raise GoogleAuthRequired(
            f"{token_path} is missing {', '.join(missing)}. Re-run: python -m o2sync auth"
        )

    return "\n".join(
        [
            f"{ENV_CLIENT_ID}={data['client_id']}",
            f"{ENV_CLIENT_SECRET}={data['client_secret']}",
            f"{ENV_REFRESH_TOKEN}={data['refresh_token']}",
        ]
    )
