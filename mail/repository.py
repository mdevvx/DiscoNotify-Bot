"""Database access for email (migration 0013): contacts, templates and the
outbox. The outbox is both the queue and the log."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from database.client import Database, is_unique_violation

_CONTACTS = "notify_email_contacts"
_TEMPLATES = "notify_email_templates"
_OUTBOX = "notify_email_outbox"
_MAX_DETAIL = 300


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat()


class MailRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ── contacts ────────────────────────────────────────────────────────

    async def contacts_for_guild(self, guild_id: int) -> List[Dict[str, Any]]:
        result = await self._db.client.table(_CONTACTS).select("*").eq("guild_id", guild_id).execute()
        return result.data

    async def is_unsubscribed(self, email: str) -> bool:
        """Whether this address opted out of reminder (marketing) emails — the
        unsubscribe link applies to every server it's a contact for."""
        result = await self._db.client.table(_CONTACTS).select("*").eq("email", email.lower()).execute()
        return any(row.get("marketing_unsubscribed_at") for row in result.data)

    # ── templates ───────────────────────────────────────────────────────

    async def get_template(self, key: str) -> Optional[Dict[str, Any]]:
        result = await self._db.client.table(_TEMPLATES).select("*").eq("key", key).execute()
        return result.data[0] if result.data else None

    # ── outbox ──────────────────────────────────────────────────────────

    async def enqueue(
        self,
        *,
        to_email: str,
        template_key: str,
        guild_id: Optional[int],
        variables: Dict[str, Any],
        dedupe_key: Optional[str],
        send_after: Optional[datetime.datetime] = None,
    ) -> bool:
        """False if this dedupe key was queued before (nothing is added)."""
        payload: Dict[str, Any] = {
            "to_email": to_email.lower(),
            "template_key": template_key,
            "guild_id": guild_id,
            "kind": "auto",
            "variables": variables,
            "dedupe_key": dedupe_key,
        }
        if send_after is not None:
            payload["send_after"] = _iso(send_after)
        try:
            await self._db.client.table(_OUTBOX).insert(payload).execute()
            return True
        except Exception as exc:
            if is_unique_violation(exc):
                return False
            raise

    async def due(self, now: datetime.datetime, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Queued emails whose time has come, oldest first."""
        result = await self._db.client.table(_OUTBOX).select("*").eq("status", "queued").execute()
        due = [row for row in result.data if _parse(row.get("send_after")) <= now]
        due.sort(key=lambda row: str(row.get("created_at") or ""))
        return due[:limit]

    async def claim(self, row_id: str) -> bool:
        """Marks a queued email as being sent; False if something else got it first."""
        result = await (
            self._db.client.table(_OUTBOX).update({"status": "sending"}).eq("id", row_id).eq("status", "queued").execute()
        )
        return bool(result.data)

    async def mark_sent(self, row_id: str, *, subject: str, provider_id: str, attempts: int, now: datetime.datetime) -> None:
        await self._update(
            row_id,
            {"status": "sent", "subject_sent": subject[:300], "provider_id": provider_id, "attempts": attempts, "sent_at": _iso(now)},
        )

    async def mark_failed(self, row_id: str, detail: str, *, attempts: int) -> None:
        await self._update(row_id, {"status": "failed", "status_detail": detail[:_MAX_DETAIL], "attempts": attempts})

    async def mark_skipped(self, row_id: str, reason: str) -> None:
        await self._update(row_id, {"status": "skipped", "status_detail": reason[:_MAX_DETAIL]})

    async def requeue(self, row_id: str, detail: str, *, attempts: int, send_after: datetime.datetime) -> None:
        await self._update(
            row_id,
            {"status": "queued", "status_detail": detail[:_MAX_DETAIL], "attempts": attempts, "send_after": _iso(send_after)},
        )

    async def _update(self, row_id: str, changes: Dict[str, Any]) -> None:
        await self._db.client.table(_OUTBOX).update(changes).eq("id", row_id).execute()


def _parse(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        moment = value
    else:
        try:
            moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)
