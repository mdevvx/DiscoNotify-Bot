"""Database access for billing (migrations 0006 and 0008): one
`notify_billing` row per server, and a `notify_payments` ledger
of every payment — written by the website (pending crypto payments) or by
the bot (card subscriptions it found at Polar)."""

from __future__ import annotations

import datetime
from typing import Any, Dict, Iterable, List, Optional

from database.client import Database, is_unique_violation

_BILLING = "notify_billing"
_PAYMENTS = "notify_payments"
_GUILDS = "notify_guilds"


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat()


class BillingRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # ── billing rows ────────────────────────────────────────────────────

    async def get(self, guild_id: int) -> Optional[Dict[str, Any]]:
        result = await self._db.client.table(_BILLING).select("*").eq("guild_id", guild_id).execute()
        return result.data[0] if result.data else None

    async def list_for_guilds(self, guild_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        ids = list(set(guild_ids))
        if not ids:
            return {}
        result = await self._db.client.table(_BILLING).select("*").in_("guild_id", ids).execute()
        return {int(row["guild_id"]): row for row in result.data}

    async def get_or_create(self, guild_id: int) -> Dict[str, Any]:
        """The server's row. A server seen for the first time gets one with
        no trial yet: it starts when someone activates it on the website
        (the `notify_activate_trial` database function). The guild row must
        already exist (notify_guilds is the foreign key)."""
        existing = await self.get(guild_id)
        if existing is not None:
            return existing
        try:
            inserted = await (
                self._db.client.table(_BILLING).insert({"guild_id": guild_id, "activation_required": True}).execute()
            )
            return inserted.data[0]
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            row = await self.get(guild_id)
            assert row is not None
            return row

    async def update(self, guild_id: int, changes: Dict[str, Any]) -> None:
        payload = {key: _iso(value) if isinstance(value, datetime.datetime) else value for key, value in changes.items()}
        await self._db.client.table(_BILLING).update(payload).eq("guild_id", guild_id).execute()

    async def list_awaiting_activation(self) -> List[Dict[str, Any]]:
        """Servers whose free trial hasn't been activated yet."""
        result = await self._db.client.table(_BILLING).select("*").eq("activation_required", True).execute()
        return [row for row in result.data if not row.get("trial_ends_at")]

    async def list_whitelisted(self) -> List[Dict[str, Any]]:
        result = await self._db.client.table(_BILLING).select("*").eq("whitelisted", True).execute()
        return result.data

    async def list_card_subscriptions(self) -> List[Dict[str, Any]]:
        """Rows with a card subscription that may still change state."""
        result = await self._db.client.table(_BILLING).select("*").execute()
        return [row for row in result.data if row.get("card_subscription_id") and row.get("card_status") != "canceled"]

    # ── payments ledger ─────────────────────────────────────────────────

    async def create_payment(
        self,
        *,
        guild_id: int,
        provider: str,
        external_id: str,
        months: int,
        amount_usd: float,
        requested_by: Optional[int],
        channel_id: Optional[int],
        checkout_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = await (
            self._db.client.table(_PAYMENTS)
            .insert(
                {
                    "guild_id": guild_id,
                    "provider": provider,
                    "external_id": external_id,
                    "months": months,
                    "amount_usd": amount_usd,
                    "requested_by": requested_by,
                    "channel_id": channel_id,
                    "checkout_url": checkout_url,
                    "metadata": metadata or {},
                }
            )
            .execute()
        )
        return result.data[0]

    async def get_payment(self, provider: str, external_id: str) -> Optional[Dict[str, Any]]:
        result = await (
            self._db.client.table(_PAYMENTS).select("*").eq("provider", provider).eq("external_id", external_id).execute()
        )
        return result.data[0] if result.data else None

    async def guild_exists(self, guild_id: int) -> bool:
        """Billing rows reference notify_guilds — only servers the bot
        has joined can be credited."""
        result = await self._db.client.table(_GUILDS).select("guild_id").eq("guild_id", guild_id).execute()
        return bool(result.data)

    async def list_pending(self) -> List[Dict[str, Any]]:
        result = await self._db.client.table(_PAYMENTS).select("*").eq("status", "pending").execute()
        return result.data

    async def set_payment_status(self, payment_id: str, status: str, *, paid_at: Optional[datetime.datetime] = None) -> None:
        changes: Dict[str, Any] = {"status": status}
        if paid_at is not None:
            changes["paid_at"] = _iso(paid_at)
        await self._db.client.table(_PAYMENTS).update(changes).eq("id", payment_id).execute()
