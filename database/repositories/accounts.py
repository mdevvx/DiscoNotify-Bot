"""Platform account access: the scheduler's "what do I poll" queries and
outcome recording, plus the guild-scoped add/remove/list operations behind
the `/account` command group.
"""

from __future__ import annotations

import datetime
from typing import List, Optional

from database.client import Database, is_unique_violation
from database.models import PlatformAccountRow
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "notify_platform_accounts"

# Consecutive poll-cycle failures (not retry attempts within one poll —
# RetryPolicy already handles those) before an account that hasn't been
# confirmed gone is flagged 'error' rather than left 'active'. Deliberately
# not a Settings field: unlike the platform-wide health thresholds, this is
# about one account's own reliability and isn't something a deployment
# realistically needs to tune.
_ERROR_STATUS_THRESHOLD = 10


class PlatformAccountRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, account_id: str) -> Optional[PlatformAccountRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("id", account_id).execute()
        return result.data[0] if result.data else None

    async def get_by_guild(self, guild_id: int, account_id: str) -> Optional[PlatformAccountRow]:
        """Like get(), but scoped to guild_id — used by guild-facing commands
        so one server can never look up (or remove) another's account row
        just by guessing/reusing a UUID."""
        result = (
            await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).eq("id", account_id).execute()
        )
        return result.data[0] if result.data else None

    async def list_by_guild(self, guild_id: int) -> List[PlatformAccountRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).execute()
        return result.data

    async def add(
        self,
        guild_id: int,
        platform: str,
        platform_account_id: str,
        username: str,
        *,
        display_name: Optional[str] = None,
    ) -> Optional[PlatformAccountRow]:
        """Returns the new row, or None if this (guild, platform, account)
        combination is already being monitored (section 37's duplicate
        prevention — enforced by the DB unique constraint, not a pre-check,
        so it's race-safe)."""
        try:
            result = await (
                self._db.client.table(_TABLE)
                .insert(
                    {
                        "guild_id": guild_id,
                        "platform": platform,
                        "platform_account_id": platform_account_id,
                        "username": username,
                        "display_name": display_name,
                    }
                )
                .execute()
            )
            return result.data[0]
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            return None

    async def remove(self, guild_id: int, account_id: str) -> bool:
        """Deletes the account (scoped to guild_id). Every child row —
        account_channels, alert_configurations, events, live_states —
        cascades via the composite foreign keys in the Phase 2 schema, so
        nothing else needs cleaning up here. Returns whether a row was
        actually deleted."""
        result = (
            await self._db.client.table(_TABLE).delete().eq("guild_id", guild_id).eq("id", account_id).execute()
        )
        return bool(result.data)

    async def list_enabled_by_platform(self, platform: str) -> List[PlatformAccountRow]:
        """Every enabled account for this platform, across ALL guilds.

        For the scheduler's use only. Two guilds watching the same channel
        are two separate rows here (see the Phase 2 schema notes) — this
        result must never be returned from a guild-facing command as-is.
        """
        result = (
            await self._db.client.table(_TABLE)
            .select("*")
            .eq("platform", platform)
            .eq("enabled", True)
            .execute()
        )
        return result.data

    async def set_content_cursor(self, account_id: str, cursor: Optional[str]) -> None:
        """Persists the adapter's opaque "newest content seen" cursor, so a
        restart resumes from where it left off instead of either re-flooding
        the back-catalog or silently missing the gap (see migration 0003)."""
        await self._db.client.table(_TABLE).update({"content_cursor": cursor}).eq("id", account_id).execute()

    async def record_success(self, account_id: str) -> None:
        await (
            self._db.client.table(_TABLE)
            .update(
                {
                    "consecutive_failures": 0,
                    "last_error": None,
                    "last_checked_at": _now_iso(),
                    "status": "active",
                }
            )
            .eq("id", account_id)
            .execute()
        )

    async def record_failure(self, account_id: str, error: str, *, status: Optional[str] = None) -> None:
        """Records a failed poll. Pass status='invalid' when the adapter has
        confirmed the account is gone (a PermanentPlatformError); otherwise
        leave it None and the account is only flagged 'error' once it's
        failed persistently, not on the first blip."""
        row = await self.get(account_id)
        if row is None:
            logger.warning("record_failure called for unknown account %s", account_id)
            return

        failures = row["consecutive_failures"] + 1
        if status is None:
            status = "error" if failures >= _ERROR_STATUS_THRESHOLD else row["status"]

        await (
            self._db.client.table(_TABLE)
            .update(
                {
                    "consecutive_failures": failures,
                    "last_error": error[:500],
                    "last_checked_at": _now_iso(),
                    "status": status,
                }
            )
            .eq("id", account_id)
            .execute()
        )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
