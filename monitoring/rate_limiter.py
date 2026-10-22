"""A sliding-window async rate limiter, shared by every worker polling the
same platform (section 24: RateLimitManager).

One instance per platform, owned by that platform's PlatformScheduler —
the limit is against the platform's API as a whole, not per-account, so
all of that platform's workers must acquire from the same instance.

Some APIs (YouTube Data API v3 in particular: search.list costs 100 quota
units, playlistItems.list costs 1) charge different amounts for different
calls against the same shared budget. `acquire`/`try_acquire` take an
optional `cost` for that — the default of 1 makes an uncosted call behave
exactly as a plain "N requests per period" limiter, so existing callers are
unaffected.
"""

from __future__ import annotations

import asyncio
import collections
import time
from typing import Deque, Tuple


class RateLimiter:
    def __init__(self, max_requests: int, period_seconds: float) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be positive")
        self._budget = max_requests
        self._period = period_seconds
        # (timestamp, cost) pairs still within the window.
        self._entries: Deque[Tuple[float, int]] = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int = 1) -> None:
        """Blocks until `cost` units are available under the budget/period window.

        Not meant for costs that make up a large fraction of the whole
        budget — the wait when the window is full can be up to `period`
        seconds. For a call that's expensive relative to its budget (and
        where blocking for that long is the wrong behavior), use
        try_acquire() and have the caller skip/defer instead.
        """
        async with self._lock:
            self._evict_expired()
            if self._used() + cost > self._budget:
                wait_time = self._period - (time.monotonic() - self._entries[0][0])
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self._evict_expired()
            self._entries.append((time.monotonic(), cost))

    async def try_acquire(self, cost: int = 1) -> bool:
        """Non-blocking: returns False immediately (consuming nothing) if
        `cost` units aren't currently available, instead of waiting."""
        async with self._lock:
            self._evict_expired()
            if self._used() + cost > self._budget:
                return False
            self._entries.append((time.monotonic(), cost))
            return True

    def used(self) -> int:
        """Units spent in the current window (read-only, never waits)."""
        self._evict_expired()
        return self._used()

    def add_usage(self, units: int) -> None:
        """Records `units` spent elsewhere (by the platform's own count, or
        before a restart) so this window agrees with it. They age out like
        any other call."""
        if units > 0:
            self._entries.append((time.monotonic(), units))

    def remaining(self) -> int:
        """Budget left in the current window (read-only, never waits)."""
        self._evict_expired()
        return max(self._budget - self._used(), 0)

    def _used(self) -> int:
        return sum(cost for _, cost in self._entries)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        while self._entries and now - self._entries[0][0] >= self._period:
            self._entries.popleft()
