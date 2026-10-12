"""Polls one platform account, in isolation from every other account.

An unhandled exception here is caught and recorded, never raised past
poll_once() — one broken account must not take down its platform's
scheduler, let alone the bot (section 9: "one problematic account must not
crash the entire monitoring service").
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Awaitable, Callable, Optional

from database.repositories.accounts import PlatformAccountRepository
from database.repositories.live_states import LiveStateRepository
from events.models import EventType, NormalizedEvent
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from platforms.base import CONTENT_CAPABILITIES, AccountRef, Capability, PlatformAdapter
from utils.errors import PermanentPlatformError, RateLimitedError
from utils.logger import get_logger

logger = get_logger(__name__)

# Used only when a RateLimitedError doesn't carry a concrete retry_after
# (the platform's response didn't include one) — better to pause for a
# sensible default than to treat "rate limited, no Retry-After" as "keep
# hammering it."
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AccountWorker:
    def __init__(
        self,
        *,
        guild_id: int,
        account_id: str,
        account_ref: AccountRef,
        adapter: PlatformAdapter,
        account_repo: PlatformAccountRepository,
        live_state_repo: LiveStateRepository,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        health_tracker: PlatformHealthTracker,
        submit_event: Callable[[NormalizedEvent], Awaitable[None]],
        initial_cursor: Optional[str] = None,
    ) -> None:
        self._guild_id = guild_id
        self._account_id = account_id
        self._account_ref = account_ref
        self._adapter = adapter
        self._account_repo = account_repo
        self._live_state_repo = live_state_repo
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._health_tracker = health_tracker
        self._submit_event = submit_event
        # Persisted in platform_accounts.content_cursor (migration 0003) so
        # a restart resumes from here instead of re-flooding or silently
        # skipping whatever was published during the downtime.
        self._cursor: Optional[str] = initial_cursor

    @property
    def platform_account_id(self) -> str:
        return self._account_ref.platform_account_id

    async def poll_once(self) -> None:
        try:
            if self._adapter.capabilities & CONTENT_CAPABILITIES:
                await self._poll_content()
            if self._adapter.supports(Capability.LIVE_STATUS):
                await self._poll_live_status()
        except Exception as exc:
            await self._handle_failure(exc)
            return

        await self._account_repo.record_success(self._account_id)
        await self._health_tracker.record_success()

    async def _poll_content(self) -> None:
        result = await self._retry_policy.run(self._fetch_updates)
        if result.next_cursor != self._cursor:
            self._cursor = result.next_cursor
            await self._account_repo.set_content_cursor(self._account_id, self._cursor)
        for raw_item in result.items:
            event = self._adapter.normalize_event(self._account_ref, raw_item)
            event = dataclasses.replace(event, guild_id=self._guild_id, account_id=self._account_id)
            await self._submit_event(event)

    async def _fetch_updates(self):
        await self._rate_limiter.acquire()
        self._health_tracker.record_request()
        return await self._adapter.fetch_updates(self._account_ref, self._cursor)

    async def _poll_live_status(self) -> None:
        status = await self._retry_policy.run(self._get_live_status)
        if status is None:
            return

        previous = await self._live_state_repo.get(self._account_id)
        was_live = bool(previous and previous["is_live"])

        if status.is_live and not was_live:
            await self._live_state_repo.set_live(
                guild_id=self._guild_id,
                account_id=self._account_id,
                stream_id=status.stream_id,
                started_at=status.started_at,
            )
            await self._submit_event(
                NormalizedEvent(
                    guild_id=self._guild_id,
                    account_id=self._account_id,
                    platform=self._account_ref.platform,
                    platform_account_id=self._account_ref.platform_account_id,
                    account_username=self._account_ref.username,
                    event_type=EventType.STREAM_STARTED,
                    # Falls back to a synthetic ID only for a platform whose
                    # API exposes no per-session stream ID (e.g. Kick) — it
                    # must vary per *session*, not just per account, or the
                    # dedup unique constraint on (account_id, event_type,
                    # platform_event_id) would silently swallow every live
                    # notification after that account's very first one.
                    platform_event_id=status.stream_id
                    or f"live-{self._account_id}-{(status.started_at or _utcnow()).isoformat()}",
                    title=status.title,
                    description=status.description,
                    url=status.url,
                    thumbnail_url=status.thumbnail_url,
                    author=status.author,
                    author_avatar_url=status.author_avatar_url,
                    started_at=status.started_at,
                    metadata={"category": status.category, "viewer_count": status.viewer_count},
                )
            )
        elif not status.is_live and was_live:
            ended_stream_id = previous["current_stream_id"] if previous else None
            await self._live_state_repo.set_offline(guild_id=self._guild_id, account_id=self._account_id)
            await self._submit_event(
                NormalizedEvent(
                    guild_id=self._guild_id,
                    account_id=self._account_id,
                    platform=self._account_ref.platform,
                    platform_account_id=self._account_ref.platform_account_id,
                    account_username=self._account_ref.username,
                    event_type=EventType.STREAM_ENDED,
                    platform_event_id=f"{ended_stream_id or self._account_id}-ended",
                )
            )
        else:
            await self._live_state_repo.touch_checked(guild_id=self._guild_id, account_id=self._account_id)

    async def _get_live_status(self):
        await self._rate_limiter.acquire()
        self._health_tracker.record_request()
        return await self._adapter.get_live_status(self._account_ref)

    async def _handle_failure(self, exc: Exception) -> None:
        if isinstance(exc, RateLimitedError):
            retry_after = exc.retry_after if exc.retry_after is not None else _DEFAULT_RATE_LIMIT_WAIT_SECONDS
            logger.warning(
                "Rate limited by %s (account %s), pausing this platform for %.0fs: %s",
                self._account_ref.platform,
                self._account_id,
                retry_after,
                exc,
            )
            # Not a per-account problem — don't count it against the
            # account's own failure/health record, only the platform's.
            await self._health_tracker.record_rate_limited(retry_after)
            return

        if isinstance(exc, PermanentPlatformError):
            logger.error(
                "Account %s (%s/%s) is invalid: %s",
                self._account_id,
                self._account_ref.platform,
                self._account_ref.username,
                exc,
            )
            await self._account_repo.record_failure(self._account_id, str(exc), status="invalid")
        else:
            logger.warning(
                "Failed to poll account %s (%s/%s): %s",
                self._account_id,
                self._account_ref.platform,
                self._account_ref.username,
                exc,
            )
            await self._account_repo.record_failure(self._account_id, str(exc))

        await self._health_tracker.record_failure(str(exc))
