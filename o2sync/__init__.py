"""Scrape planned services from the O2 customer extranet (client.o2.fr).

This package currently covers the read side only: authenticate, fetch the
planning, and normalise it into stable records. Pushing those records into a
Google Calendar is the next step and is intentionally not implemented yet.
"""

from .client import O2Client
from .errors import (
    AccountDisabled,
    AuthError,
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
    "O2Client",
    "Service",
    "Worker",
    "normalize_all",
    "normalize_event",
    "O2Error",
    "AuthError",
    "InvalidCredentials",
    "AccountDisabled",
    "NotACustomerAccount",
    "PasswordUpdateRequired",
    "ServiceUnavailable",
    "TransportError",
    "__version__",
]
