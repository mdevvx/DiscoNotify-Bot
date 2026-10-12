"""Runs polling for every enabled account of ONE platform, on a shared
interval and a shared RateLimiter (the limit is against that platform's API
as a whole, not per-account). The interval is the adapter's own choice when
it has one (PlatformAdapter.poll_interval_seconds()), re-evaluated every
cycle as accounts come and go.

Re-reads the enabled-account list from the database at the start of every
cycle, so accounts added/removed/toggled between cycles are picked up
without a bot restart.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict

from database.repositories.accounts import PlatformAccountRepository
from database.repositories.live_states import LiveStateRepository
from events.models import NormalizedEvent
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from monitoring.worker import AccountWorker
from platforms.base import AccountRef, PlatformAdapter
from utils.logger import get_logger

logger = get_logger(__name__)


class PlatformScheduler:
    def __init__(
        self,
        adapter: PlatformAdapter,
        *,
        poll_interval_seconds: float,
        account_repo: PlatformAccountRepository,
        live_state_repo: LiveStateRepository,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        health_tracker: PlatformHealthTracker,
        submit_event: Callable[[NormalizedEvent], Awaitable[None]],
    ) -> None:
        self._adapter = adapter
        self._default_poll_interval = poll_interval_seconds
        self._poll_interval = poll_interval_seconds
        self._account_repo = account_repo
        self._live_state_repo = live_state_repo
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._health_tracker = health_tracker
        self._submit_event = submit_event

        self._workers: Dict[str, AccountWorker] = {}
        self._task: "asyncio.Task[None] | None" = None
        self._stop_event = asyncio.Event()

    @property
    def platform(self) -> str:
        return self._adapter.platform

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        await self._sync_workers()
        self._update_poll_interval()
        self._task = asyncio.create_task(self._run_loop(), name=f"scheduler-{self._adapter.platform}")
        logger.info("Started %s scheduler (%d account(s), every %.0fs).", self._adapter.platform, self.worker_count, self._poll_interval)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._poll_all()
            await self._health_tracker.flush()
            self._update_poll_interval()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_all(self) -> None:
        if self._health_tracker.is_rate_limited():
            # A worker hit a 429 last cycle — skip this platform entirely
            # rather than immediately hitting the same limit again. Other
            # platforms' schedulers are unaffected (section: one platform's
            # quota must never compromise monitoring of another).
            logger.debug("Skipping %s poll cycle — rate limited.", self._adapter.platform)
            return

        await self._sync_workers()
        if not self._workers:
            return

        results = await asyncio.gather(
            *(worker.poll_once() for worker in self._workers.values()),
            return_exceptions=True,
        )
        for account_id, result in zip(self._workers.keys(), results):
            if isinstance(result, BaseException):
                # AccountWorker.poll_once() already catches and records
                # everything it can classify — reaching here means something
                # genuinely unexpected slipped through, so it's logged but
                # still must not stop the other accounts from being polled.
                logger.error("Unexpected error polling account %s: %s", account_id, result, exc_info=result)

    def _update_poll_interval(self) -> None:
        distinct_accounts = len({worker.platform_account_id for worker in self._workers.values()})
        interval = self._adapter.poll_interval_seconds(distinct_accounts)
        interval = interval if interval is not None else self._default_poll_interval
        if interval != self._poll_interval:
            logger.info(
                "%s poll interval is now %.0fs (%d distinct account(s)).",
                self._adapter.platform,
                interval,
                distinct_accounts,
            )
            self._poll_interval = interval

    async def _sync_workers(self) -> None:
        rows = await self._account_repo.list_enabled_by_platform(self._adapter.platform)
        current_ids = {row["id"] for row in rows}

        for stale_id in set(self._workers) - current_ids:
            del self._workers[stale_id]

        for row in rows:
            if row["id"] in self._workers:
                continue
            account_ref = AccountRef(
                platform=row["platform"],
                platform_account_id=row["platform_account_id"],
                username=row["username"],
            )
            self._workers[row["id"]] = AccountWorker(
                guild_id=row["guild_id"],
                account_id=row["id"],
                account_ref=account_ref,
                adapter=self._adapter,
                account_repo=self._account_repo,
                live_state_repo=self._live_state_repo,
                rate_limiter=self._rate_limiter,
                retry_policy=self._retry_policy,
                health_tracker=self._health_tracker,
                submit_event=self._submit_event,
                initial_cursor=row.get("content_cursor"),
            )
