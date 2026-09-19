"""Exception hierarchy for the Kalshi client.

Every failure mode the data layer must distinguish gets its own type, so collectors can
decide between *retry*, *quarantine the message*, *resync the book* and *abort*.
"""

from __future__ import annotations


class KalshiError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(KalshiError):
    """Missing or invalid configuration (e.g. credentials not set)."""


class AuthenticationError(KalshiError):
    """Credentials missing, malformed, or rejected by the exchange (HTTP 401/403)."""


class TransportError(KalshiError):
    """Network-level failure that persisted through all retries."""


class APIError(KalshiError):
    """The exchange answered with a non-success HTTP status."""

    def __init__(self, status_code: int, message: str, *, url: str = "", body: object = None):
        super().__init__(f"HTTP {status_code} from {url or 'API'}: {message}")
        self.status_code = status_code
        self.message = message
        self.url = url
        self.body = body


class NotFoundError(APIError):
    """HTTP 404."""


class RateLimitError(APIError):
    """HTTP 429 that persisted through all retries."""


class ServerError(APIError):
    """HTTP 5xx that persisted through all retries."""


class MessageValidationError(KalshiError):
    """A payload failed schema/units validation.  Carries the raw payload for quarantine."""

    def __init__(self, message: str, *, raw: object = None):
        super().__init__(message)
        self.raw = raw


class SequenceGapError(KalshiError):
    """A WebSocket sequence number skipped ahead: at least one message was lost."""

    def __init__(self, sid: int, expected: int, got: int):
        super().__init__(f"sid={sid}: expected seq {expected}, got {got}")
        self.sid, self.expected, self.got = sid, expected, got
