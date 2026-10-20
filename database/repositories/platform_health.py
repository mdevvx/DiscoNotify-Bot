"""The bot's own API usage/health against each platform, backed by
`platform_health` — the one table in this schema that is NOT guild-scoped
(see the Phase 2 migration notes). One row per platform, created on first
use rather than pre-seeded.
"""

from __future__ import annotations

import datetime
from typing import Optional

from database.client import Database, is_unique_violation
from database.models import PlatformHealthRow
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "notify_platform_health"


class PlatformHealthRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, platform: str) -> Optional[PlatformHealthRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("platform", platform).execute()
        return result.data[0] if result.data else None

    async def record_success(self, platform: str) -> None:
        await self._upsert(platform, {"status": "healthy", "last_success_at": _now_iso(), "consecutive_failures": 0})

    async def record_failure(self, platform: str, error: str, *, degrade_after: int, error_after: int) -> None:
        row = await self._get_or_create(platform)
        failures = row["consecutive_failures"] + 1
        status = "healthy"
        if failures >= error_after:
            status = "error"
        elif failures >= degrade_after:
            status = "degraded"
        await self._upsert(
            platform,
            {
                "status": status,
                "last_error_at": _now_iso(),
                "last_error": error[:500],
                "consecutive_failures": failures,
            },
        )

    async def record_rate_limited(self, platform: str, retry_after_seconds: float) -> None:
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=retry_after_seconds)
        await self._upsert(platform, {"status": "degraded", "rate_limited_until": until.isoformat()})

    async def set_quota(self, platform: str, *, remaining: Optional[int], limit: Optional[int]) -> None:
        """Only called where the provider actually reports quota (section 46:
        never fabricate this for a platform whose API doesn't expose it)."""
        await self._upsert(platform, {"quota_remaining": remaining, "quota_limit": limit})

    async def increment_requests(self, platform: str, by: int = 1) -> None:
        if by <= 0:
            return
        row = await self._get_or_create(platform)
        today = datetime.date.today().isoformat()
        if row["requests_today_reset_at"] != today:
            await self._upsert(platform, {"requests_today": by, "requests_today_reset_at": today})
        else:
            # Read-modify-write, not atomic under concurrent callers for the
            # same platform. Acceptable here: PlatformHealthTracker batches
            # this to one flush per scheduler tick rather than per request,
            # so the race window in practice is a handful of schedulers (one
            # per platform) each writing once every poll interval, not a
            # high-concurrency counter. A Postgres RPC (atomic increment)
            # would be the fix if that ever stops being true.
            await self._upsert(platform, {"requests_today": row["requests_today"] + by})

    async def _get_or_create(self, platform: str) -> PlatformHealthRow:
        existing = await self.get(platform)
        if existing is not None:
            return existing
        try:
            inserted = await self._db.client.table(_TABLE).insert({"platform": platform}).execute()
            return inserted.data[0]
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            existing = await self.get(platform)
            if existing is not None:
                return existing
            raise

    async def _upsert(self, platform: str, fields: dict) -> None:
        existing = await self.get(platform)
        if existing is not None:
            await self._db.client.table(_TABLE).update(fields).eq("platform", platform).execute()
            return
        try:
            await self._db.client.table(_TABLE).insert({"platform": platform, **fields}).execute()
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            await self._db.client.table(_TABLE).update(fields).eq("platform", platform).execute()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
