"""PulseNotify's own exception hierarchy.

Notification-specific exceptions are added once Phase 4 has a real sender
to raise them.
"""

from __future__ import annotations

from typing import Optional


class PulseNotifyError(Exception):
    """Base class for all PulseNotify-specific errors."""


class ConfigurationError(PulseNotifyError):
    """Required configuration is missing or invalid."""


class DatabaseError(PulseNotifyError):
    """A database access problem that isn't itself a Supabase/Postgrest error."""


class PlatformError(PulseNotifyError):
    """Base class for errors a platform adapter raises while talking to its API.

    `message` is for the log. `user_message`, when given, is a plain-English
    sentence that's safe to show members (see utils/user_messages.py) — use it
    only for answers the member can act on, like "that account doesn't exist".
    """

    def __init__(self, message: str = "", *, user_message: Optional[str] = None) -> None:
        super().__init__(message)
        self.user_message = user_message


class TransientPlatformError(PlatformError):
    """A retryable failure: timeout, 5xx, or a temporary network issue."""


class RateLimitedError(TransientPlatformError):
    """The platform responded with a 429 (or equivalent rate-limit signal).

    Adapters raise this instead of a plain TransientPlatformError when they
    can tell it's specifically a rate limit — it's still retryable, but the
    worker handles it by pausing that whole platform's scheduler for
    `retry_after` seconds (via PlatformHealthTracker) instead of just
    burning through RetryPolicy's normal backoff, which would keep hitting
    the same limit.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentPlatformError(PlatformError):
    """A non-retryable failure: account not found, revoked credentials, bad request.

    RetryPolicy re-raises this immediately instead of burning through retry
    attempts on something that will never succeed.
    """
