"""Exceptions raised by the O2 client.

They are split by cause so the CLI can pick a meaningful exit code and, more
importantly, so a scheduled run can tell "retry later" apart from "stop, a human
has to do something".
"""


class O2Error(Exception):
    """Base class for every error raised by this package."""


class TransportError(O2Error):
    """Network failure, timeout, or an HTTP status we could not recover from."""


class AuthError(O2Error):
    """Authentication failed. A human has to intervene; retrying will not help."""


class InvalidCredentials(AuthError):
    """The e-mail/password pair was rejected (``state: unknown``)."""


class AccountDisabled(AuthError):
    """The customer account is deactivated (``state: customerActive``)."""


class NotACustomerAccount(AuthError):
    """An employee login was used instead of a customer one (``state: notCustomer``)."""


class PasswordUpdateRequired(AuthError):
    """O2 wants the password rotated before it will serve the account.

    The site drives this through a browser flow, so the only fix is to log in at
    https://client.o2.fr/ once and set a new password.
    """


class ServiceUnavailable(O2Error):
    """O2's own backend is down (``state: keycloak_unavailable``, or ``fail``).

    Transient by nature: a later run is likely to succeed.
    """


class SessionExpired(O2Error):
    """The PHP session is no longer authenticated. Internal; triggers one re-login."""


class CalendarError(Exception):
    """Base class for the Google Calendar side."""


class GoogleAuthRequired(CalendarError):
    """No usable Google credentials.

    Raised instead of opening a browser: ``sync`` has to be safe to run from cron,
    so the one-time consent lives in the ``auth`` subcommand only.
    """


class CalendarAPIError(CalendarError):
    """The Google Calendar API rejected a request we could not recover from."""
