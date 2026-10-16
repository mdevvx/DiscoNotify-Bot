"""Per-guild settings, scoped strictly by guild_id.

This is the first of several repositories (accounts, channels, alert
configs, events, ... land in later phases) but the isolation rule set here
applies to all of them: every query is scoped by guild_id, and nothing here
ever loads rows across guilds and filters in application code.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

from database.client import Database, is_unique_violation
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "pulsenotify_guilds"


class GuildRepository:
    """Reads and writes per-guild settings.

    Enabled-state lookups are cached in memory because the command tree
    checks this on every single interaction. The cache is updated per-guild
    by set_enabled() rather than invalidated globally, so one guild's
    toggle can never leak into another guild's cached state.
    """

    def __init__(self, database: Database) -> None:
        self._db = database
        self._enabled_cache: Dict[int, bool] = {}

    async def is_enabled(self, guild_id: int) -> bool:
        if guild_id in self._enabled_cache:
            return self._enabled_cache[guild_id]

        record = await self._get_or_create(guild_id)
        enabled = bool(record["enabled"])
        self._enabled_cache[guild_id] = enabled
        return enabled

    async def set_enabled(self, guild_id: int, enabled: bool) -> None:
        await self._get_or_create(guild_id)
        await (
            self._db.client.table(_TABLE)
            .update({"enabled": enabled})
            .eq("guild_id", guild_id)
            .execute()
        )
        self._enabled_cache[guild_id] = enabled

    async def set_present(self, guild_id: int, present: bool) -> None:
        """Whether the bot is in this server now. Settings are kept either
        way; the website's server list uses this."""
        await self._get_or_create(guild_id)
        await self._db.client.table(_TABLE).update({"bot_present": present}).eq("guild_id", guild_id).execute()

    async def sync_presence(self, present_ids: Iterable[int]) -> None:
        """At startup: servers the bot is in are present, every other known
        server is not. Only rows that changed are written."""
        present = set(present_ids)
        rows = (await self._db.client.table(_TABLE).select("guild_id, bot_present").execute()).data
        known = {int(row["guild_id"]): bool(row.get("bot_present", True)) for row in rows}
        for guild_id in present - known.keys():
            await self.set_present(guild_id, True)
        for guild_id, was_present in known.items():
            if was_present != (guild_id in present):
                await self.set_present(guild_id, guild_id in present)

    async def _get_or_create(self, guild_id: int) -> Dict[str, Any]:
        result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).execute()
        if result.data:
            return result.data[0]

        try:
            inserted = await self._db.client.table(_TABLE).insert({"guild_id": guild_id}).execute()
            return inserted.data[0]
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            # Another concurrent request (e.g. two interactions at once) created
            # the row first — re-read instead of treating this as a failure.
            result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).execute()
            if result.data:
                return result.data[0]
            raise
