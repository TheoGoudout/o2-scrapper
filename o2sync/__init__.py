"""Mirror planned services from the O2 customer extranet into a Google Calendar.

Read side: :mod:`o2sync.client` authenticates against client.o2.fr and fetches the
planning, :mod:`o2sync.model` normalises it into stable records.

Write side: :mod:`o2sync.sync` decides what to create, update and delete (pure
logic, no network), :mod:`o2sync.gcal` performs it against Google Calendar, and
:mod:`o2sync.gauth` handles credentials.
"""

from .client import O2Client
from .errors import (
    AccountDisabled,
    AuthError,
    CalendarAPIError,
    CalendarError,
    GoogleAuthRequired,
    InvalidCredentials,
    NotACustomerAccount,
    O2Error,
    PasswordUpdateRequired,
    ServiceUnavailable,
    TransportError,
)
from .model import Service, Worker, normalize_all, normalize_event

__version__ = "0.1.0"

__all__ = [
    "AccountDisabled",
    "AuthError",
    "CalendarAPIError",
    "CalendarError",
    "GoogleAuthRequired",
    "InvalidCredentials",
    "NotACustomerAccount",
    "O2Client",
    "O2Error",
    "PasswordUpdateRequired",
    "Service",
    "ServiceUnavailable",
    "TransportError",
    "Worker",
    "__version__",
    "normalize_all",
    "normalize_event",
]
