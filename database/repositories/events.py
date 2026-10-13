"""The event deduplication ledger (section 10), backed by the `events` table.

record_if_new() is the entire dedup mechanism: the table's unique
constraint on (account_id, event_type, platform_event_id) is what actually
prevents duplicates (including across a bot restart or two overlapping
polls racing each other) — this just turns "insert, and tell me if it was
actually new" into a clean return value instead of exception-handling at
every call site.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from database.client import Database, is_unique_violation
from database.models import EventRow
from utils.logger import get_logger

logger = get_logger(__name__)

_TABLE = "pulsenotify_events"


class EventRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def record_if_new(
        self,
        *,
        guild_id: int,
        account_id: str,
        platform: str,
        event_type: str,
        platform_event_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[EventRow]:
        """Inserts the event and returns its row, or None if it's already been recorded."""
        try:
            result = await (
                self._db.client.table(_TABLE)
                .insert(
                    {
                        "guild_id": guild_id,
                        "account_id": account_id,
                        "platform": platform,
                        "event_type": event_type,
                        "platform_event_id": platform_event_id,
                        "metadata": metadata or {},
                    }
                )
                .execute()
            )
            return result.data[0]
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            return None

    async def last_notified_by_account(self, guild_id: int, *, scan_limit: int = 500) -> Dict[str, str]:
        """account_id -> when its most recent alert went out (ISO time),
        for one guild. Scans only the guild's latest `scan_limit` alerts —
        an account with nothing among those just shows no "last alert"."""
        result = await (
            self._db.client.table(_TABLE)
            .select("account_id,notified_at")
            .eq("guild_id", guild_id)
            .eq("notified", True)
            .order("notified_at", desc=True)
            .limit(scan_limit)
            .execute()
        )
        latest: Dict[str, str] = {}
        for row in result.data:
            if row.get("notified_at"):
                latest.setdefault(row["account_id"], row["notified_at"])
        return latest

    async def mark_notified(self, event_id: str) -> None:
        await (
            self._db.client.table(_TABLE)
            .update({"notified": True, "notified_at": _now_iso()})
            .eq("id", event_id)
            .execute()
        )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
