"""A quota counter for an API whose limit resets once a day at midnight
Pacific time (YouTube Data API v3).

Unlike RateLimiter's rolling window, this matches how the provider counts:
units used since the last Pacific midnight, back to zero at the next one. It
can be saved and reloaded, so a restart doesn't make the bot forget what it
already spent today (the provider's counter doesn't reset when the bot does).

Pacific time is worked out here rather than with `zoneinfo`, which needs the
`tzdata` package on Windows: the US rule (daylight time from the second
Sunday of March, 2:00 local, to the first Sunday of November, 2:00 local) has
been fixed since 2007.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, Mapping, Optional

_UTC = datetime.timezone.utc


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _nth_sunday(year: int, month: int, n: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def pacific_offset(moment: datetime.datetime) -> datetime.timedelta:
    """UTC-7 during US daylight time, UTC-8 otherwise."""
    moment = moment.astimezone(_UTC)
    start = datetime.datetime.combine(_nth_sunday(moment.year, 3, 2), datetime.time(10), tzinfo=_UTC)  # 2:00 PST
    end = datetime.datetime.combine(_nth_sunday(moment.year, 11, 1), datetime.time(9), tzinfo=_UTC)  # 2:00 PDT
    return datetime.timedelta(hours=-7 if start <= moment < end else -8)


def pacific_date(moment: datetime.datetime) -> datetime.date:
    return (moment.astimezone(_UTC) + pacific_offset(moment)).date()


def seconds_until_pacific_midnight(moment: datetime.datetime) -> float:
    """Until the provider's quota resets."""
    local = moment.astimezone(_UTC) + pacific_offset(moment)
    next_midnight_local = datetime.datetime.combine(local.date() + datetime.timedelta(days=1), datetime.time(0))
    next_midnight_utc = (next_midnight_local - pacific_offset(moment)).replace(tzinfo=_UTC)
    return max((next_midnight_utc - moment.astimezone(_UTC)).total_seconds(), 0.0)


class DailyQuota:
    def __init__(self, limit: int, *, clock: Callable[[], datetime.datetime] = _utcnow) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._clock = clock
        self._day = pacific_date(clock())
        self._used = 0
        self._dirty = False

    @property
    def limit(self) -> int:
        return self._limit

    def used(self) -> int:
        self._roll_over()
        return self._used

    def remaining(self) -> int:
        self._roll_over()
        return max(self._limit - self._used, 0)

    async def try_acquire(self, cost: int = 1) -> bool:
        """Spends `cost` units if today's budget has them; False (spending
        nothing) if not."""
        self._roll_over()
        if self._used + cost > self._limit:
            return False
        self._used += cost
        self._dirty = True
        return True

    def exhaust(self) -> None:
        """The provider says today's quota is gone: stop spending until the reset."""
        self._roll_over()
        if self._used < self._limit:
            self._used = self._limit
            self._dirty = True

    def restore(self, state: Optional[Mapping[str, Any]]) -> None:
        """Reload what `take_state()` saved. Ignored unless it's from today
        (Pacific), and never lowers what's already been counted."""
        self._roll_over()
        if not isinstance(state, Mapping) or state.get("day") != self._day.isoformat():
            return
        used = state.get("used")
        if isinstance(used, int) and not isinstance(used, bool) and used > self._used:
            self._used = min(used, self._limit)

    def take_state(self) -> Optional[Dict[str, Any]]:
        """What to save, if anything changed since the last call."""
        self._roll_over()
        if not self._dirty:
            return None
        self._dirty = False
        return {"day": self._day.isoformat(), "used": self._used}

    def mark_dirty(self) -> None:
        """Saving failed — offer the state again next time."""
        self._dirty = True

    def _roll_over(self) -> None:
        today = pacific_date(self._clock())
        if today != self._day:
            self._day = today
            self._used = 0
            self._dirty = True
