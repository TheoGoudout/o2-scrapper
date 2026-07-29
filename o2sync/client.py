"""HTTP client for the O2 customer extranet (client.o2.fr).

The extranet is a WordPress site whose planning screen is driven entirely by
``admin-ajax.php``, so there is no HTML to parse: every call below returns JSON or
a short text token. The endpoints and parameter names were read off the site's own
production JavaScript (``/wp-content/themes/extranet-client/js/scripts_common.js``)
and then verified against a live account.

Endpoints used, all ``POST`` to ``/wp-admin/admin-ajax.php``:

===========================  ====================================================
``ask_login``                ``login``, ``pwd``, ``updatePwd`` -> ``{"state": …}``
``get_planning_events``      ``startDate``, ``endDate`` (epoch ms) -> JSON array
``get_hs_type_label``        ``HsTypeShortname`` -> plain text label
``get_hs_level_label``       ``HsLevelShortname`` -> plain text label
``ask_logout``               (none)
===========================  ====================================================

The session is a plain ``PHPSESSID`` cookie and there is no CSRF nonce.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .errors import (
    AccountDisabled,
    InvalidCredentials,
    NotACustomerAccount,
    PasswordUpdateRequired,
    ServiceUnavailable,
    SessionExpired,
    TransportError,
)

log = logging.getLogger(__name__)

BASE_URL = "https://client.o2.fr"
AJAX_PATH = "/wp-admin/admin-ajax.php"
LOGIN_PATH = "/connexion/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: ``(connect, read)`` timeouts. The planning call is the slow one.
DEFAULT_TIMEOUT = (10, 45)

#: The backend answers a dead session, or its own upstream failing, with this token.
FAIL_TOKEN = "fail"

#: ``ask_login`` states that are not a success, mapped to the exception to raise.
LOGIN_FAILURES = {
    "unknown": (InvalidCredentials, "O2 rejected the e-mail/password pair."),
    "customerActive": (AccountDisabled, "This O2 account is deactivated; contact your agency."),
    "notCustomer": (
        NotACustomerAccount,
        "This is an employee login. Use the e-mail of your O2 customer account.",
    ),
    "UPDATE_PWD": (
        PasswordUpdateRequired,
        "O2 requires a password change. Log in at https://client.o2.fr/ once to set a new one.",
    ),
    "keycloak_unavailable": (
        ServiceUnavailable,
        "O2's authentication backend is unavailable; try again later.",
    ),
}


def _build_retry(total: int):
    """Retry policy for connection errors and 5xx, including on POST.

    Every endpoint here is a read, so retrying a POST is safe.
    """
    from urllib3.util.retry import Retry

    kwargs: dict[str, Any] = {
        "total": total,
        "connect": total,
        "read": total,
        "status": total,
        "backoff_factor": 1.0,
        "status_forcelist": (500, 502, 503, 504),
        "raise_on_status": False,
    }
    try:
        return Retry(allowed_methods=frozenset(["GET", "POST"]), **kwargs)
    except TypeError:  # urllib3 < 1.26 spells it differently
        return Retry(method_whitelist=frozenset(["GET", "POST"]), **kwargs)


class O2Client:
    """A logged-in session against the O2 extranet.

    Usable as a context manager, which logs out on the way out::

        with O2Client() as client:
            client.login(email, password)
            events = client.fetch_events(start, end)
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
        retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Origin": self.base_url,
                "Referer": self.base_url + LOGIN_PATH,
            }
        )
        adapter = HTTPAdapter(max_retries=_build_retry(retries))
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._credentials: tuple[str, str] | None = None
        self._type_labels: dict[str, str] = {}
        self._level_labels: dict[str, str] = {}

    # ------------------------------------------------------------------ plumbing

    def _post(self, action: str, **params: Any) -> str:
        """POST an admin-ajax action and return the raw response body."""
        data = {"action": action, **params}
        try:
            response = self.session.post(
                self.base_url + AJAX_PATH, data=data, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TransportError(f"{action}: request failed ({exc})") from exc

        if response.status_code != 200:
            raise TransportError(f"{action}: HTTP {response.status_code}")

        # WordPress serves this endpoint without a charset; the payload is UTF-8.
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = "utf-8"
        return response.text.strip()

    @staticmethod
    def _parse_json(action: str, body: str) -> Any:
        if body == FAIL_TOKEN:
            raise SessionExpired(f"{action}: backend returned '{FAIL_TOKEN}'")
        try:
            return json.loads(body)
        except ValueError as exc:
            snippet = body[:200].replace("\n", " ")
            raise TransportError(f"{action}: unexpected non-JSON response: {snippet!r}") from exc

    # --------------------------------------------------------------------- auth

    def login(self, email: str, password: str) -> None:
        """Authenticate and keep the session cookie for subsequent calls."""
        # Fetching the login page first gets us a PHPSESSID, exactly as a browser
        # would; admin-ajax will otherwise mint one mid-login.
        try:
            self.session.get(self.base_url + LOGIN_PATH, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TransportError(f"could not reach {self.base_url}{LOGIN_PATH} ({exc})") from exc

        body = self._post(
            "ask_login",
            login=email,
            pwd=password,
            # The site sends `true` here when the password fails its strength
            # policy, which pushes the user into a forced-rotation flow. We never
            # want to trigger that from a script.
            updatePwd="false",
        )

        try:
            payload = self._parse_json("ask_login", body)
        except SessionExpired as exc:
            raise ServiceUnavailable("O2 refused the login request; try again later.") from exc

        state = (payload or {}).get("state") if isinstance(payload, dict) else None
        if state == "success":
            self._credentials = (email, password)
            log.info("Logged in to %s as %s", self.base_url, email)
            return

        if state in LOGIN_FAILURES:
            exc_type, message = LOGIN_FAILURES[state]
            raise exc_type(message)

        raise ServiceUnavailable(f"Unexpected login state {state!r} from O2.")

    def logout(self) -> None:
        """End the session. Best effort: failures here are never worth raising."""
        try:
            self._post("ask_logout")
        except Exception as exc:  # noqa: BLE001 - logout must not mask real errors
            log.debug("Logout failed, ignoring: %s", exc)

    def _relogin(self) -> bool:
        if not self._credentials:
            return False
        log.info("Session expired; logging in again.")
        self.login(*self._credentials)
        return True

    # ------------------------------------------------------------------ planning

    def fetch_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return the raw planning events between two datetimes.

        Timestamps go out as epoch milliseconds; the site pads the start with
        ``000`` and the end with ``999`` to cover the whole second, and we do the
        same.
        """
        if end <= start:
            raise ValueError("end must be after start")

        start_ms = int(start.timestamp()) * 1000
        end_ms = int(end.timestamp()) * 1000 + 999

        try:
            body = self._post(
                "get_planning_events", startDate=str(start_ms), endDate=str(end_ms)
            )
            payload = self._parse_json("get_planning_events", body)
        except SessionExpired:
            # Either the session lapsed or O2's backend hiccuped; one retry tells
            # us which, and costs a single extra request.
            if not self._relogin():
                raise ServiceUnavailable(
                    "O2 returned 'fail' for the planning request (not logged in)."
                )
            body = self._post(
                "get_planning_events", startDate=str(start_ms), endDate=str(end_ms)
            )
            try:
                payload = self._parse_json("get_planning_events", body)
            except SessionExpired as exc:
                raise ServiceUnavailable(
                    "O2 kept returning 'fail' for the planning request; try again later."
                ) from exc

        if payload is None:
            return []
        if not isinstance(payload, list):
            raise TransportError(
                f"get_planning_events: expected a list, got {type(payload).__name__}"
            )
        return [item for item in payload if isinstance(item, dict)]

    def fetch_events_chunked(
        self, start: datetime, end: datetime, chunk_days: int | None = None
    ) -> list[dict[str, Any]]:
        """Same as :meth:`fetch_events`, optionally split into smaller windows.

        A single call covering five years was verified to return exactly the same
        events as 59 chunked calls, so chunking is off by default. It stays
        available in case an account with far more history hits a server-side cap.
        """
        if not chunk_days or chunk_days <= 0:
            return self.fetch_events(start, end)

        from datetime import timedelta

        seen: dict[str, dict[str, Any]] = {}
        cursor = start
        step = timedelta(days=chunk_days)
        while cursor < end:
            window_end = min(cursor + step, end)
            for item in self.fetch_events(cursor, window_end):
                seen[str(item.get("eventId"))] = item
            cursor = window_end
        return list(seen.values())

    # -------------------------------------------------------------------- labels

    def _label(self, action: str, param: str, code: str, cache: dict[str, str]) -> str:
        """Ask the server to translate a shorthand code.

        The endpoint echoes the code back when it does not know it, so an unknown
        code is indistinguishable from a valid one — which is fine, both are
        usable as a label. A failure here must never break a run.
        """
        if not code:
            return ""
        if code in cache:
            return cache[code]
        try:
            label = self._post(action, **{param: code})
        except Exception as exc:  # noqa: BLE001 - labels are cosmetic
            log.debug("Label lookup %s(%s) failed: %s", action, code, exc)
            return ""
        if not label or label == FAIL_TOKEN or "<" in label:
            # A truncated session gives us an HTML error page rather than a label.
            return ""
        cache[code] = label
        return label

    def hs_type_label(self, code: str) -> str:
        return self._label("get_hs_type_label", "HsTypeShortname", code, self._type_labels)

    def hs_level_label(self, code: str) -> str:
        return self._label("get_hs_level_label", "HsLevelShortname", code, self._level_labels)

    def resolve_labels(self, events: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
        """Fetch the labels for every distinct type/level found in ``events``."""
        types = {str(e.get("hsType") or "") for e in events} - {""}
        levels = {str(e.get("hsLevel") or "") for e in events} - {""}
        type_labels = {code: self.hs_type_label(code) for code in sorted(types)}
        level_labels = {code: self.hs_level_label(code) for code in sorted(levels)}
        return (
            {k: v for k, v in type_labels.items() if v},
            {k: v for k, v in level_labels.items() if v},
        )

    # ---------------------------------------------------------------- lifecycle

    def __enter__(self) -> "O2Client":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._credentials:
            self.logout()
        self.session.close()
