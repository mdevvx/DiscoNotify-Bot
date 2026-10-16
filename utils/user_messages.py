"""What members are told when something goes wrong.

Members never see technical text — no status codes, API names, settings or
raw error strings. They get a short plain-English sentence, and anything
unexpected also gets a short reference code; the full detail goes to the
log under that same code, so the bot's owner can look it up.

Adapters can attach a sentence meant for members to an error
(`PermanentPlatformError(message, user_message="…")`) — used for "that
account doesn't exist" style answers, where saying *what* is wrong helps.
Everything else is replaced by a generic line here.
"""

from __future__ import annotations

import math
import uuid
from typing import Optional

from utils.errors import PermanentPlatformError, PlatformError, RateLimitedError
from utils.logger import get_logger

logger = get_logger(__name__)


def new_reference() -> str:
    """A short code to quote to support, e.g. `a1b2c3d4`."""
    return uuid.uuid4().hex[:8]


def minutes_text(seconds: Optional[float]) -> str:
    minutes = max(1, math.ceil((seconds or 60) / 60))
    return f"about {minutes} minute{'s' if minutes != 1 else ''}"


def friendly_platform_error(
    error: BaseException, *, platform: str, context: str, not_found: Optional[str] = None
) -> str:
    """A member-safe sentence for an error from a platform or a payment
    provider. The detail is logged with a reference code, which is part of
    the sentence when the cause isn't something the member can act on."""
    if isinstance(error, RateLimitedError):
        return f"{platform} is busy right now. Please try again in {minutes_text(error.retry_after)}."

    user_message = getattr(error, "user_message", None)
    if user_message:
        return user_message

    reference = new_reference()
    logger.warning("%s failed [ref %s]: %s: %s", context, reference, type(error).__name__, error)
    if isinstance(error, PermanentPlatformError):
        text = not_found or f"We couldn't find that {platform} account. Please check it and try again."
        return f"{text} (Reference `{reference}`)"
    if isinstance(error, PlatformError):
        return f"{platform} isn't responding right now. Please try again in a few minutes. (Reference `{reference}`)"
    logger.error("%s failed unexpectedly [ref %s]", context, reference, exc_info=error)
    return f"Something went wrong on our side. Please try again in a moment. (Reference `{reference}`)"
