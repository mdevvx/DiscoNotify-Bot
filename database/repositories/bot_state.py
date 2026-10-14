"""Bot-wide key → JSON settings that survive restarts (migration 0007).
Not guild-scoped — these describe the bot as a whole (e.g. maintenance)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from database.client import Database

_TABLE = "pulsenotify_bot_state"


class BotStateRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        result = await self._db.client.table(_TABLE).select("*").eq("key", key).execute()
        return result.data[0]["value"] if result.data else None

    async def set(self, key: str, value: Dict[str, Any]) -> None:
        existing = await self._db.client.table(_TABLE).select("key").eq("key", key).execute()
        if existing.data:
            await self._db.client.table(_TABLE).update({"value": value}).eq("key", key).execute()
        else:
            await self._db.client.table(_TABLE).insert({"key": key, "value": value}).execute()

    async def delete(self, key: str) -> None:
        await self._db.client.table(_TABLE).delete().eq("key", key).execute()
