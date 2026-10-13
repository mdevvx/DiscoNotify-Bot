"""Current live/offline status per account, backed by the `live_states` table.

This is what makes section 8 work: the worker reads get() before deciding
whether a fresh "is_live=true" snapshot represents a *new* stream (fire
STREAM_STARTED) or one it already announced (do nothing) — including across
a bot restart, since the row survives it.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from database.client import Database, is_unique_violation
from database.models import LiveStateRow
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "pulsenotify_live_states"


class LiveStateRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def list_for_guild(self, guild_id: int) -> Dict[str, LiveStateRow]:
        """account_id -> its live state, for one guild."""
        result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).execute()
        return {row["account_id"]: row for row in result.data}

    async def get(self, account_id: str) -> Optional[LiveStateRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("account_id", account_id).execute()
        return result.data[0] if result.data else None

    async def set_live(
        self,
        *,
        guild_id: int,
        account_id: str,
        stream_id: Optional[str],
        started_at: Optional[datetime.datetime],
    ) -> None:
        await self._upsert(
            guild_id,
            account_id,
            {
                "is_live": True,
                "current_stream_id": stream_id,
                "started_at": started_at.isoformat() if started_at else _now_iso(),
                "ended_at": None,
                "last_checked_at": _now_iso(),
            },
        )

    async def set_offline(self, *, guild_id: int, account_id: str) -> None:
        await self._upsert(
            guild_id,
            account_id,
            {
                "is_live": False,
                "ended_at": _now_iso(),
                "last_checked_at": _now_iso(),
            },
        )

    async def touch_checked(self, *, guild_id: int, account_id: str) -> None:
        await self._upsert(guild_id, account_id, {"last_checked_at": _now_iso()})

    async def _upsert(self, guild_id: int, account_id: str, fields: Dict[str, Any]) -> None:
        existing = await self.get(account_id)
        if existing is not None:
            await self._db.client.table(_TABLE).update(fields).eq("account_id", account_id).execute()
            return

        try:
            await (
                self._db.client.table(_TABLE)
                .insert({"account_id": account_id, "guild_id": guild_id, **fields})
                .execute()
            )
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            # Lost a race with a concurrent poll of the same account — the
            # other insert already recorded a state, so just apply our update.
            await self._db.client.table(_TABLE).update(fields).eq("account_id", account_id).execute()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
