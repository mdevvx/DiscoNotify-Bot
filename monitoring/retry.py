"""Exponential backoff with jitter, retrying transient failures and giving
up immediately on permanent ones (section 22: "do not retry permanent
errors indefinitely").
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from utils.errors import PermanentPlatformError, PlatformPaused, RateLimitedError
from utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 2.0
    max_delay: float = 300.0
    jitter_fraction: float = 0.1

    async def run(self, func: Callable[[], Awaitable[T]], *, max_attempts: Optional[int] = None) -> T:
        """`max_attempts` overrides this policy's own for one call — a
        platform whose every request spends a scarce quota (Instagram,
        YouTube) asks for fewer retries than a cheap one."""
        limit = self.max_attempts if max_attempts is None else max(max_attempts, 1)
        attempt = 0
        while True:
            try:
                return await func()
            except (PermanentPlatformError, RateLimitedError, PlatformPaused):
                # Permanent errors will never succeed; rate limits need to
                # wait out an actual Retry-After, not a short generic
                # backoff — both are handled by the caller, not retried here.
                raise
            except Exception as exc:
                attempt += 1
                if attempt >= limit:
                    raise
                delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                delay += random.uniform(0, delay * self.jitter_fraction)
                logger.warning(
                    "Retrying after error (attempt %d/%d, waiting %.1fs): %s",
                    attempt,
                    limit,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
