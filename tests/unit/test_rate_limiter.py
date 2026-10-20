"""RateLimiter is a real sliding-window limiter, not a mock — these tests
use small real time windows rather than a fake clock, since asyncio's
scheduling is what's actually under test."""

from __future__ import annotations

import asyncio
import time

from monitoring.rate_limiter import RateLimiter


async def test_requests_within_budget_do_not_wait() -> None:
    limiter = RateLimiter(max_requests=3, period_seconds=1.0)

    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.2


async def test_exceeding_the_budget_waits_for_the_window_to_free_up() -> None:
    limiter = RateLimiter(max_requests=2, period_seconds=0.3)

    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()  # third request within the window must wait
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25  # allow a little scheduling slack under 0.3s


async def test_concurrent_acquires_are_serialized_not_all_allowed_through() -> None:
    limiter = RateLimiter(max_requests=1, period_seconds=0.3)
    order = []

    async def _use(name: str) -> None:
        await limiter.acquire()
        order.append(name)

    await asyncio.gather(_use("a"), _use("b"))

    assert order == ["a", "b"] or order == ["b", "a"]
    assert len(order) == 2


async def test_weighted_cost_is_counted_against_the_same_budget() -> None:
    """Models YouTube's actual quota: one expensive call (cost=100) can
    exhaust a budget that would otherwise allow many cheap calls (cost=1)."""
    limiter = RateLimiter(max_requests=100, period_seconds=1.0)

    assert await limiter.try_acquire(cost=100) is True
    assert await limiter.try_acquire(cost=1) is False


async def test_try_acquire_does_not_block_and_does_not_consume_budget_on_rejection() -> None:
    limiter = RateLimiter(max_requests=1, period_seconds=5.0)

    assert await limiter.try_acquire() is True
    start = time.monotonic()
    allowed = await limiter.try_acquire()
    elapsed = time.monotonic() - start

    assert allowed is False
    assert elapsed < 0.05  # must return immediately, not wait for the window


async def test_try_acquire_succeeds_again_once_the_window_frees_up() -> None:
    limiter = RateLimiter(max_requests=1, period_seconds=0.1)

    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is False

    await asyncio.sleep(0.15)

    assert await limiter.try_acquire() is True


async def test_remaining_shows_the_unspent_budget_and_recovers_when_the_window_passes() -> None:
    limiter = RateLimiter(max_requests=5, period_seconds=0.2)
    assert limiter.remaining() == 5

    await limiter.acquire(cost=2)
    assert limiter.remaining() == 3

    await asyncio.sleep(0.25)
    assert limiter.remaining() == 5
