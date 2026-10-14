"""Per-platform health/usage tracking (section 29, and the API usage
manager called for explicitly alongside the Phase 2 schema work).

Request counts are batched in memory and flushed to `platform_health` once
per scheduler tick rather than on every single API call — one platform's
scheduler polls at most a few times a minute, so a per-request DB write
would be pure overhead for no accuracy benefit.
"""

from __future__ import annotations

import time
from typing import Optional

from database.repositories.platform_health import PlatformHealthRepository
from utils.logger import get_logger

logger = get_logger(__name__)


class PlatformHealthTracker:
    def __init__(
        self,
        platform: str,
        repository: PlatformHealthRepository,
        *,
        degrade_after: int,
        error_after: int,
    ) -> None:
        self._platform = platform
        self._repo = repository
        self._degrade_after = degrade_after
        self._error_after = error_after
        self._pending_requests = 0
        # In-memory, monotonic-clock deadline checked by the scheduler
        # before every poll cycle — this is what actually makes a 429
        # pause polling rather than just being recorded for observability.
        self._rate_limited_until: Optional[float] = None

    def record_request(self) -> None:
        """Call once per outbound platform API request (typically right
        alongside RateLimiter.acquire())."""
        self._pending_requests += 1

    def record_requests(self, count: int) -> None:
        """Several requests at once — e.g. an adapter's own count of the real
        API calls it made this cycle."""
        self._pending_requests += max(count, 0)

    async def flush(self) -> None:
        if self._pending_requests:
            await self._repo.increment_requests(self._platform, by=self._pending_requests)
            self._pending_requests = 0

    async def record_success(self) -> None:
        await self._repo.record_success(self._platform)

    async def record_failure(self, error: str) -> None:
        await self._repo.record_failure(
            self._platform, error, degrade_after=self._degrade_after, error_after=self._error_after
        )

    async def record_rate_limited(self, retry_after_seconds: float) -> None:
        self._rate_limited_until = time.monotonic() + max(retry_after_seconds, 0.0)
        await self._repo.record_rate_limited(self._platform, retry_after_seconds)

    def is_rate_limited(self) -> bool:
        return self._rate_limited_until is not None and time.monotonic() < self._rate_limited_until

    async def set_quota(self, *, remaining: Optional[int], limit: Optional[int]) -> None:
        await self._repo.set_quota(self._platform, remaining=remaining, limit=limit)
