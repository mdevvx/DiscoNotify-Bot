"""Per-account, per-event-type alert preferences, backed by
`alert_configurations` (section 7).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from database.client import Database, is_unique_violation
from database.models import AlertConfigurationRow
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "pulsenotify_alert_configurations"

# Sensible defaults seeded when an account is first added (section 7's
# example: "Live Ended: OFF" while everything else defaults on). A missing
# event type here defaults to enabled=True via DEFAULT_ENABLED.get(...).
DEFAULT_ENABLED: Dict[str, bool] = {
    "post_created": True,
    "video_published": True,
    "short_published": True,
    "stream_started": True,
    "stream_updated": True,
    "stream_ended": False,
    "account_updated": False,
}


class AlertConfigurationRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def seed_defaults(self, guild_id: int, account_id: str, event_types: Iterable[str]) -> None:
        for event_type in event_types:
            try:
                await (
                    self._db.client.table(_TABLE)
                    .insert(
                        {
                            "guild_id": guild_id,
                            "account_id": account_id,
                            "event_type": event_type,
                            "enabled": DEFAULT_ENABLED.get(event_type, True),
                        }
                    )
                    .execute()
                )
            except Exception as exc:
                if not is_unique_violation(exc):
                    raise  # a config row for this (account, event_type) already exists — fine

    async def get(self, account_id: str, event_type: str) -> Optional[AlertConfigurationRow]:
        result = (
            await self._db.client.table(_TABLE)
            .select("*")
            .eq("account_id", account_id)
            .eq("event_type", event_type)
            .execute()
        )
        return result.data[0] if result.data else None

    async def accounts_with_every_alert_off(self, account_ids: Iterable[str]) -> Set[str]:
        """Of these accounts, the ones that have alert settings and every one
        of them is switched off. An account with no settings at all isn't
        included — the sender treats "no settings" as "alerts on"."""
        ids = list(account_ids)
        if not ids:
            return set()
        result = await self._db.client.table(_TABLE).select("account_id,enabled").in_("account_id", ids).execute()
        any_on: Dict[str, bool] = {}
        for row in result.data:
            any_on[row["account_id"]] = any_on.get(row["account_id"], False) or bool(row["enabled"])
        return {account_id for account_id, on in any_on.items() if not on}

    async def list_for_guild(self, guild_id: int) -> List[AlertConfigurationRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("guild_id", guild_id).execute()
        return result.data

    async def list_for_account(self, account_id: str) -> List[AlertConfigurationRow]:
        result = await self._db.client.table(_TABLE).select("*").eq("account_id", account_id).execute()
        return result.data

    async def set_custom_message(
        self, account_id: str, message: Optional[str], *, event_type: Optional[str] = None
    ) -> int:
        """Sets (or, with message=None, clears) the custom message template
        for an account. Applies to every event type currently configured
        for that account, unless `event_type` narrows it to just one.
        Returns how many rows were updated (0 means the account/event_type
        combination doesn't exist — the caller should treat that as "not
        found," not silently succeed)."""
        query = self._db.client.table(_TABLE).update({"custom_message": message}).eq("account_id", account_id)
        if event_type is not None:
            query = query.eq("event_type", event_type)
        result = await query.execute()
        return len(result.data)

    async def set_mention_role(
        self, account_id: str, mention_role_id: Optional[int], *, event_type: Optional[str] = None
    ) -> int:
        """Sets (or, with mention_role_id=None, clears) the role mentioned
        when this account posts. Applies to every event type currently
        configured for the account, unless `event_type` narrows it to just
        one — mirrors set_custom_message()'s shape exactly. Returns how
        many rows were updated (0 means the account/event_type combination
        doesn't exist)."""
        query = self._db.client.table(_TABLE).update({"mention_role_id": mention_role_id}).eq("account_id", account_id)
        if event_type is not None:
            query = query.eq("event_type", event_type)
        result = await query.execute()
        return len(result.data)

    async def set_alert_toggle(
        self,
        account_id: str,
        event_type: str,
        *,
        enabled: Optional[bool] = None,
        embed_enabled: Optional[bool] = None,
    ) -> int:
        """Updates `enabled` and/or `embed_enabled` for one (account,
        event_type) row — whichever of the two is passed as not-None.
        Unlike set_custom_message()/set_mention_role(), this always
        targets exactly one event type (there's no "apply to every event
        type" mode) since turning an alert on/off is inherently a
        per-event-type decision, not an account-wide one. Returns how many
        rows were updated (0 means that account/event_type combination
        doesn't exist)."""
        changes: Dict[str, Any] = {}
        if enabled is not None:
            changes["enabled"] = enabled
        if embed_enabled is not None:
            changes["embed_enabled"] = embed_enabled
        if not changes:
            return 0

        result = (
            await self._db.client.table(_TABLE)
            .update(changes)
            .eq("account_id", account_id)
            .eq("event_type", event_type)
            .execute()
        )
        return len(result.data)
