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
import time
from typing import Awaitable, Callable, Dict, Optional, Set

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

# A platform that asks for spread-out polling (PlatformAdapter.spread_polls)
# starts its accounts' checks across this fraction of the poll interval, with
# at most _MAX_SPREAD_GAP_SECONDS between two of them.
_SPREAD_FRACTION = 0.5
_MAX_SPREAD_GAP_SECONDS = 30.0
_MIN_WAIT_SECONDS = 1.0


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
        guild_filter: Optional[Callable[[Set[int]], Awaitable[Set[int]]]] = None,
        muted_accounts: Optional[Callable[[Set[str]], Awaitable[Set[str]]]] = None,
    ) -> None:
        self._adapter = adapter
        # Which servers may be monitored right now (not paused with
        # /notify toggle, and with billing access) — see
        # MonitoringManager.guild_filter. None = every server.
        self._guild_filter = guild_filter
        # Accounts whose every alert type is switched off — checking them
        # would spend API calls on alerts nobody receives.
        self._muted_accounts = muted_accounts
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
    def poll_interval(self) -> float:
        return self._poll_interval

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

    async def _report_quota(self) -> None:
        """Writes the adapter's remaining quota, where it has one, for /status
        and the admin panel. Never lets a failure here stop polling."""
        try:
            snapshot = self._adapter.quota_snapshot()
            if snapshot is not None:
                await self._health_tracker.set_quota(remaining=snapshot[0], limit=snapshot[1])
        except Exception:
            logger.warning("Couldn't record %s quota.", self._adapter.platform, exc_info=True)

    async def _persist_state(self) -> None:
        """Lets the adapter save what it keeps across restarts. Never lets a
        failure here stop polling."""
        try:
            await self._adapter.persist_state()
        except Exception:
            logger.warning("Couldn't save %s state.", self._adapter.platform, exc_info=True)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            await self._poll_all()
            elapsed = time.monotonic() - started
            if self._adapter.counts_own_requests:
                self._health_tracker.record_requests(self._adapter.take_request_count())
            await self._health_tracker.flush()
            await self._report_quota()
            await self._persist_state()
            self._update_poll_interval()
            # The time spent spreading the checks out counts toward the interval.
            wait = self._poll_interval
            if self._adapter.spread_polls:
                wait = max(self._poll_interval - elapsed, min(self._poll_interval, _MIN_WAIT_SECONDS))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
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

        gap = self._spread_gap(len(self._workers))
        results = await asyncio.gather(
            *(self._poll_after(worker, index * gap) for index, worker in enumerate(self._workers.values())),
            return_exceptions=True,
        )
        for account_id, result in zip(self._workers.keys(), results):
            if isinstance(result, BaseException):
                # AccountWorker.poll_once() already catches and records
                # everything it can classify — reaching here means something
                # genuinely unexpected slipped through, so it's logged but
                # still must not stop the other accounts from being polled.
                logger.error("Unexpected error polling account %s: %s", account_id, result, exc_info=result)

    def _spread_gap(self, worker_count: int) -> float:
        """Seconds between one account's check starting and the next's — 0
        for a platform that doesn't ask for spreading."""
        if not self._adapter.spread_polls or worker_count < 2:
            return 0.0
        return min(self._poll_interval * _SPREAD_FRACTION / worker_count, _MAX_SPREAD_GAP_SECONDS)

    async def _poll_after(self, worker: AccountWorker, delay: float) -> None:
        if delay > 0:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return  # shutting down
            except asyncio.TimeoutError:
                pass
        await worker.poll_once()

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
        if self._guild_filter is not None and rows:
            try:
                allowed = await self._guild_filter({int(row["guild_id"]) for row in rows})
                rows = [row for row in rows if int(row["guild_id"]) in allowed]
            except Exception:
                # A failing check must not stop monitoring for servers that
                # are entitled to it — keep everyone this cycle.
                logger.warning("Server filter failed for %s; polling every account this cycle.", self._adapter.platform, exc_info=True)
        if self._muted_accounts is not None and rows:
            try:
                muted = await self._muted_accounts({row["id"] for row in rows})
                rows = [row for row in rows if row["id"] not in muted]
            except Exception:
                logger.warning("Alert-settings check failed for %s; polling every account this cycle.", self._adapter.platform, exc_info=True)
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
