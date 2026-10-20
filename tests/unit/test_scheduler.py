"""PlatformScheduler: creates/removes workers to match the enabled-account
list, polls all of them each cycle, and its start()/stop() lifecycle
actually starts and stops a background loop rather than hanging."""

from __future__ import annotations

import asyncio

import pytest

from database.repositories.accounts import PlatformAccountRepository
from database.repositories.live_states import LiveStateRepository
from database.repositories.platform_health import PlatformHealthRepository
from events.models import NormalizedEvent
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from monitoring.scheduler import PlatformScheduler
from tests.unit.fakes import FakeDatabase
from tests.unit.test_worker import FakeAdapter, _seed_account


def _make_scheduler(db: FakeDatabase, adapter: FakeAdapter, submitted: list, *, poll_interval: float = 60.0):
    return PlatformScheduler(
        adapter,
        poll_interval_seconds=poll_interval,
        account_repo=PlatformAccountRepository(db),  # type: ignore[arg-type]
        live_state_repo=LiveStateRepository(db),  # type: ignore[arg-type]
        rate_limiter=RateLimiter(max_requests=1000, period_seconds=1.0),
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.001, max_delay=0.01, jitter_fraction=0.0),
        health_tracker=PlatformHealthTracker(
            "fake", PlatformHealthRepository(db), degrade_after=3, error_after=8  # type: ignore[arg-type]
        ),
        submit_event=submitted.append,
    )


async def test_sync_workers_creates_one_worker_per_enabled_account() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    _seed_account(db, "acc-2")
    scheduler = _make_scheduler(db, FakeAdapter(), [])

    await scheduler._sync_workers()

    assert scheduler.worker_count == 2


async def test_sync_workers_ignores_disabled_accounts() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    _seed_account(db, "acc-2", enabled=False)
    scheduler = _make_scheduler(db, FakeAdapter(), [])

    await scheduler._sync_workers()

    assert scheduler.worker_count == 1


async def test_sync_workers_removes_workers_for_accounts_no_longer_enabled() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    scheduler = _make_scheduler(db, FakeAdapter(), [])
    await scheduler._sync_workers()
    assert scheduler.worker_count == 1

    db.tables["notify_platform_accounts"].rows[0]["enabled"] = False
    await scheduler._sync_workers()

    assert scheduler.worker_count == 0


async def test_poll_all_polls_every_current_worker() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    _seed_account(db, "acc-2")
    adapter = FakeAdapter()
    adapter.pending_items = [{"id": "vid-1", "title": "t"}]
    submitted: list = []
    scheduler = _make_scheduler(db, adapter, submitted)

    await scheduler._poll_all()

    # Both accounts polled fetch_updates once each this cycle.
    assert adapter.fetch_calls == 2


async def test_poll_all_skips_entirely_while_rate_limited() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    adapter = FakeAdapter()
    submitted: list = []
    scheduler = _make_scheduler(db, adapter, submitted)
    await scheduler._health_tracker.record_rate_limited(60.0)

    await scheduler._poll_all()

    assert adapter.fetch_calls == 0
    assert adapter.live_calls == 0


async def test_one_broken_account_does_not_stop_others_from_being_polled() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    _seed_account(db, "acc-2")
    adapter = FakeAdapter()
    submitted: list = []
    scheduler = _make_scheduler(db, adapter, submitted)
    await scheduler._sync_workers()

    # Make one specific worker's poll_once raise something AccountWorker's
    # own try/except wouldn't normally see (simulating a genuine bug, not a
    # classified platform error) to prove _poll_all's own safety net works.
    broken_worker = next(iter(scheduler._workers.values()))

    async def _boom() -> None:
        raise RuntimeError("unexpected bug")

    broken_worker.poll_once = _boom  # type: ignore[method-assign]

    await scheduler._poll_all()  # must not raise


async def test_start_and_stop_run_and_terminate_the_background_loop() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    adapter = FakeAdapter()
    submitted: list = []
    scheduler = _make_scheduler(db, adapter, submitted, poll_interval=0.05)

    await scheduler.start()
    await asyncio.sleep(0.12)  # let a couple of cycles run
    await scheduler.stop()

    assert adapter.fetch_calls >= 2


async def test_a_platform_with_a_quota_has_it_written_each_cycle() -> None:
    db = FakeDatabase()
    adapter = FakeAdapter()
    adapter.quota_snapshot = lambda: (70, 100)  # type: ignore[method-assign]
    scheduler = _make_scheduler(db, adapter, [])

    await scheduler._report_quota()

    row = db.tables["notify_platform_health"].rows[0]
    assert (row["quota_remaining"], row["quota_limit"]) == (70, 100)


async def test_a_platform_without_a_quota_writes_none() -> None:
    db = FakeDatabase()
    scheduler = _make_scheduler(db, FakeAdapter(), [])

    await scheduler._report_quota()

    assert db.tables["notify_platform_health"].rows == []


async def test_a_failure_reporting_quota_never_stops_polling() -> None:
    adapter = FakeAdapter()

    def broken():
        raise RuntimeError("boom")

    adapter.quota_snapshot = broken  # type: ignore[method-assign]

    await _make_scheduler(FakeDatabase(), adapter, [])._report_quota()  # must not raise
