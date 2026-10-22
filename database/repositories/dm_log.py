"""Log of every DM the bot attempts to a member (migration 0011). Rows hold
IDs, kind and status only — never message text or emails."""

from __future__ import annotations

import datetime
from typing import Optional

from database.client import Database

_TABLE = "notify_dm_log"
_MAX_ERROR = 200


class DmLogRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def add(
        self,
        *,
        guild_id: int,
        user_id: int,
        kind: str,
        status: str,
        error: Optional[str] = None,
        payment_id: Optional[str] = None,
    ) -> None:
        await (
            self._db.client.table(_TABLE)
            .insert(
                {
                    "guild_id": guild_id,
                    "discord_user_id": user_id,
                    "kind": kind,
                    "status": status,
                    "error": error[:_MAX_ERROR] if error else None,
                    "payment_id": payment_id,
                }
            )
            .execute()
        )

    async def was_sent(self, payment_id: str, kind: str) -> bool:
        """Whether a DM of this kind was already attempted for this payment
        (any outcome — a closed-DMs member isn't retried)."""
        result = await self._db.client.table(_TABLE).select("id").eq("payment_id", payment_id).eq("kind", kind).execute()
        return bool(result.data)

    async def status_of(self, guild_id: int, kind: str) -> Optional[str]:
        """The outcome of the latest DM of this kind about this server
        (sent / failed / dm_closed), or None if none was attempted."""
        result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).eq("kind", kind).execute()
        if not result.data:
            return None
        return str(max(result.data, key=lambda row: str(row.get("created_at") or ""))["status"])

    async def purge_older_than(self, days: int) -> None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        await self._db.client.table(_TABLE).delete().lt("created_at", cutoff.isoformat()).execute()
