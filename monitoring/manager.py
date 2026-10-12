"""The single entry point bot/client.py uses to start/stop all platform
monitoring (section 9's "Monitoring Manager").

Owns one PlatformScheduler per registered adapter, plus the shared
EventProcessor every scheduler feeds into. Registering a new platform is
just register_adapter(SomeAdapter(...)) — scheduling, rate limiting,
retries, and dedup all just work without this file changing.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from config.settings import Settings
from database.client import Database
from database.models import PlatformHealthRow
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.events import EventRepository
from database.repositories.live_states import LiveStateRepository
from database.repositories.platform_health import PlatformHealthRepository
from events.models import NormalizedEvent
from events.processor import EventProcessor, NotificationSink
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from monitoring.scheduler import PlatformScheduler
from platforms.base import Capability, PlatformAdapter
from utils.logger import get_logger

logger = get_logger(__name__)


async def _log_only_sink(event: NormalizedEvent) -> None:
    """Default notification sink when the caller doesn't supply a real one
    (e.g. in tests) — just logs. bot/client.py passes a real
    NotificationSender.handle in production."""
    logger.info(
        "Event ready to notify: guild=%s platform=%s type=%s title=%r",
        event.guild_id,
        event.platform,
        event.event_type.value,
        event.title,
    )


class MonitoringManager:
    def __init__(
        self, database: Database, settings: Settings, notification_sink: Optional[NotificationSink] = None
    ) -> None:
        self._settings = settings
        self._account_repo = PlatformAccountRepository(database)
        self._live_state_repo = LiveStateRepository(database)
        self._health_repo = PlatformHealthRepository(database)
        self._event_processor = EventProcessor(
            EventRepository(database),
            notification_sink or _log_only_sink,
            queue_size=settings.monitoring_event_queue_size,
        )
        self._schedulers: Dict[str, PlatformScheduler] = {}
        self._adapters: Dict[str, PlatformAdapter] = {}
        self._started = False

    def register_adapter(self, adapter: PlatformAdapter) -> None:
        """Wires up a new platform. Call before start() — see bot/client.py."""
        if adapter.platform in self._schedulers:
            raise ValueError(f"A scheduler for platform {adapter.platform!r} is already registered.")

        poll_interval = (
            self._settings.monitoring_live_poll_interval_seconds
            if adapter.supports(Capability.LIVE_STATUS)
            else self._settings.monitoring_content_poll_interval_seconds
        )
        health_tracker = PlatformHealthTracker(
            adapter.platform,
            self._health_repo,
            degrade_after=self._settings.monitoring_health_degraded_after,
            error_after=self._settings.monitoring_health_error_after,
        )
        self._schedulers[adapter.platform] = PlatformScheduler(
            adapter,
            # Only a default — an adapter with its own quota math overrides
            # it per cycle (PlatformAdapter.poll_interval_seconds()).
            poll_interval_seconds=poll_interval,
            account_repo=self._account_repo,
            live_state_repo=self._live_state_repo,
            rate_limiter=RateLimiter(
                self._settings.monitoring_rate_limit_requests, self._settings.monitoring_rate_limit_period_seconds
            ),
            retry_policy=RetryPolicy(
                max_attempts=self._settings.monitoring_max_retry_attempts,
                base_delay=self._settings.monitoring_retry_base_delay_seconds,
                max_delay=self._settings.monitoring_retry_max_delay_seconds,
            ),
            health_tracker=health_tracker,
            submit_event=self._event_processor.submit,
        )
        self._adapters[adapter.platform] = adapter
        logger.info("Registered %s platform adapter.", adapter.platform)

    async def start(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.check_credentials()
            except Exception:
                logger.warning("Credential check for %s failed.", adapter.platform, exc_info=True)
        self._event_processor.start()
        await asyncio.gather(*(scheduler.start() for scheduler in self._schedulers.values()))
        self._started = True
        logger.info("Monitoring manager started (%d platform(s) registered).", len(self._schedulers))

    async def stop(self) -> None:
        if not self._started:
            return
        await asyncio.gather(*(scheduler.stop() for scheduler in self._schedulers.values()), return_exceptions=True)
        await self._event_processor.stop()
        await asyncio.gather(*(adapter.aclose() for adapter in self._adapters.values()), return_exceptions=True)
        self._started = False
        logger.info("Monitoring manager stopped.")

    def get_adapter(self, platform: str) -> Optional[PlatformAdapter]:
        """Used by /account add to validate/register against whichever
        adapter is actually available — returns None if that platform
        isn't configured (e.g. no API key), so the command can say so
        instead of failing with an unrelated error."""
        return self._adapters.get(platform)

    async def health_snapshot(self) -> Dict[str, Optional[PlatformHealthRow]]:
        """Platform name -> its platform_health row (None if it's never
        made a request yet, e.g. right after startup) — for /status. Only
        covers registered platforms; nothing is fabricated for platforms
        that aren't configured at all."""
        return {platform: await self._health_repo.get(platform) for platform in self._adapters}

    @property
    def registered_platforms(self) -> Dict[str, int]:
        """Platform name -> number of accounts currently being polled. For /status."""
        return {platform: scheduler.worker_count for platform, scheduler in self._schedulers.items()}
