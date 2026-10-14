"""Per-server subscriptions: access checks, the bot-owner whitelist,
confirming payments made on the website, and upgrade reminders.

Pricing: BILLING_PRICE_USD per month ($2.99 by default). Payments happen
on the PulseNotify website (BILLING_SUBSCRIBE_URL), never inside Discord:
`/pulsenotify subscribe` just links there with the server ID filled in.
Card (Paddle) renews monthly by itself; crypto (NOWPayments) is prepaid in
3/6/12-month blocks.

Nothing is enforced while `enabled` is False (BILLING_ENABLED unset):
every server has access, no trial clock starts, and no billing rows are
written.

The bot learns about website payments by polling, not webhooks (it has no
public URL), and always confirms them with the provider itself:
- card: the website opens Paddle checkout with customData.guild_id; the
  poller lists Paddle's paid transactions and picks those up;
- crypto: the website creates the NOWPayments payment and inserts a
  `pending` row in pulsenotify_payments (guild_id, provider 'nowpayments',
  external_id = payment_id, months, amount_usd); the poller checks it with
  NOWPayments and credits the months once it's paid. A pending Paddle row
  (external_id = transaction ID) works the same way.
"""

from __future__ import annotations

import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from billing.access import Access, AccessReason, evaluate, parse_time, utcnow
from billing.nowpayments import EXPIRED_STATUSES, FAILED_STATUSES, PAID_STATUSES, NowPaymentsClient
from billing.paddle import PaddleClient
from billing.repository import BillingRepository
from utils.logger import get_logger

logger = get_logger(__name__)

DAYS_PER_MONTH = 30

# A card checkout nobody completed, or a crypto payment never sent, stops
# being checked after this long.
_CARD_CHECKOUT_TTL = datetime.timedelta(hours=24)
_CRYPTO_PAYMENT_TTL = datetime.timedelta(days=3)
_CARD_REFRESH_EVERY = datetime.timedelta(minutes=10)
_ACCESS_CACHE_TTL = datetime.timedelta(seconds=60)
# On startup, look this far back for website card checkouts — covers the
# bot being offline for a while. Already-credited ones are skipped.
_CARD_DISCOVERY_LOOKBACK = datetime.timedelta(days=3)
_ACTIVE_CARD_STATUSES = frozenset({"active", "past_due", "trialing"})

PaymentCallback = Callable[[Dict[str, Any], Access], Awaitable[None]]


def format_usd(amount: float) -> str:
    """$1, $3, $2.50 — whole dollars without cents."""
    return f"${amount:.0f}" if float(amount).is_integer() else f"${amount:.2f}"


def with_server(url: Optional[str], guild_id: int) -> Optional[str]:
    """The website link for one server: `{guild_id}` in the URL is replaced
    with the server ID, otherwise `?guild_id=<id>` is added."""
    if not url:
        return None
    if "{guild_id}" in url:
        return url.replace("{guild_id}", str(guild_id))
    parts = urlsplit(url)
    query = "&".join(part for part in (parts.query, urlencode({"guild_id": guild_id})) if part)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class BillingService:
    def __init__(
        self,
        repo: BillingRepository,
        *,
        enabled: bool,
        price_usd: float = 2.99,
        trial_days: int = 7,
        grace_days: int = 3,
        subscribe_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        reminder_every_days: int = 7,
        reminder_max: int = 3,
        paddle: Optional[PaddleClient] = None,
        nowpayments: Optional[NowPaymentsClient] = None,
        clock: Callable[[], datetime.datetime] = utcnow,
    ) -> None:
        self._repo = repo
        self.enabled = enabled
        self.price_usd = round(price_usd, 2)
        self._trial = datetime.timedelta(days=trial_days)
        self._grace = datetime.timedelta(days=grace_days)
        self._subscribe_url = subscribe_url or None
        self._cancel_url = cancel_url or None
        self._reminder_every = datetime.timedelta(days=reminder_every_days)
        self._reminder_max = reminder_max
        self._paddle = paddle
        self._nowpayments = nowpayments
        self._clock = clock
        self._cache: Dict[int, Tuple[datetime.datetime, Access]] = {}
        self._last_card_refresh: Optional[datetime.datetime] = None
        self._card_discovery_since: Optional[datetime.datetime] = None
        self.on_payment: Optional[PaymentCallback] = None

    @property
    def price_label(self) -> str:
        """"$1", "$2.50" — for every place the price is shown."""
        return format_usd(self.price_usd)

    @property
    def card_available(self) -> bool:
        return self._paddle is not None

    @property
    def crypto_available(self) -> bool:
        return self._nowpayments is not None

    def subscribe_url(self, guild_id: int) -> Optional[str]:
        """The website's payment page for this server (None until set)."""
        return with_server(self._subscribe_url, guild_id)

    def cancel_url(self, guild_id: int) -> Optional[str]:
        """The website's cancellation page for this server (None until set)."""
        return with_server(self._cancel_url, guild_id)

    async def aclose(self) -> None:
        for client in (self._paddle, self._nowpayments):
            if client is not None:
                await client.aclose()

    # ── access ──────────────────────────────────────────────────────────

    async def access(self, guild_id: int) -> Access:
        if not self.enabled:
            return Access(True, AccessReason.BILLING_OFF)
        now = self._clock()
        cached = self._cache.get(guild_id)
        if cached is not None and now - cached[0] < _ACCESS_CACHE_TTL:
            return cached[1]
        row = await self._repo.get_or_create(guild_id, trial_ends_at=now + self._trial)
        result = evaluate(row, now=now, grace=self._grace)
        self._cache[guild_id] = (now, result)
        return result

    async def allowed_guild_ids(self, guild_ids: Iterable[int]) -> Set[int]:
        """Which of these servers may be monitored right now — one query
        for the lot, used by the monitoring scheduler every cycle."""
        ids = set(guild_ids)
        if not self.enabled or not ids:
            return ids
        now = self._clock()
        rows = await self._repo.list_for_guilds(ids)
        allowed = set()
        for guild_id in ids:
            row = rows.get(guild_id) or await self._repo.get_or_create(guild_id, trial_ends_at=now + self._trial)
            result = evaluate(row, now=now, grace=self._grace)
            self._cache[guild_id] = (now, result)
            if result.allowed:
                allowed.add(guild_id)
        return allowed

    async def billing_row(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get(guild_id)

    def _invalidate(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)

    # ── whitelist (bot owner only — enforced by the commands) ───────────

    async def whitelist(self, guild_id: int, *, by: int, note: Optional[str]) -> None:
        await self._repo.get_or_create(guild_id, trial_ends_at=self._clock() + self._trial)
        await self._repo.update(
            guild_id,
            {"whitelisted": True, "whitelisted_by": by, "whitelist_note": note, "whitelisted_at": self._clock()},
        )
        self._invalidate(guild_id)

    async def unwhitelist(self, guild_id: int) -> bool:
        row = await self._repo.get(guild_id)
        if row is None or not row.get("whitelisted"):
            return False
        await self._repo.update(guild_id, {"whitelisted": False})
        self._invalidate(guild_id)
        return True

    async def list_whitelisted(self) -> List[Dict[str, Any]]:
        return await self._repo.list_whitelisted()

    # ── upgrade reminders (DM to the server owner) ──────────────────────

    async def reminders_due(self, guild_ids: Iterable[int]) -> List[Dict[str, Any]]:
        """Billing rows of servers whose trial or subscription has run out
        and whose owner should get an upgrade reminder now: right away,
        then every `reminder_every_days`, at most `reminder_max` times.
        Servers that have access again get their count reset, so the next
        lapse is reminded about from the start."""
        if not self.enabled:
            return []
        now = self._clock()
        due = []
        for guild_id, row in (await self._repo.list_for_guilds(guild_ids)).items():
            sent = int(row.get("reminders_sent") or 0)
            if evaluate(row, now=now, grace=self._grace).allowed:
                if sent:
                    await self._repo.update(guild_id, {"reminders_sent": 0, "last_reminder_at": None})
                continue
            last = parse_time(row.get("last_reminder_at"))
            if sent == 0 or (sent < self._reminder_max and last is not None and now - last >= self._reminder_every):
                due.append(row)
        return due

    async def mark_reminded(self, row: Dict[str, Any]) -> None:
        await self._repo.update(
            int(row["guild_id"]),
            {"reminders_sent": int(row.get("reminders_sent") or 0) + 1, "last_reminder_at": self._clock()},
        )

    # ── poller ──────────────────────────────────────────────────────────

    async def poll_once(self) -> None:
        for payment in await self._repo.list_pending():
            try:
                if payment["provider"] == "paddle":
                    await self._check_card_checkout(payment)
                elif payment["provider"] == "nowpayments":
                    await self._check_crypto_payment(payment)
            except Exception:
                logger.warning("Couldn't check payment %s.", payment.get("id"), exc_info=True)

        if self._paddle is None:
            return
        try:
            await self._discover_card_checkouts()
        except Exception:
            logger.warning("Couldn't look for new card checkouts.", exc_info=True)

        now = self._clock()
        if self._last_card_refresh is None or now - self._last_card_refresh >= _CARD_REFRESH_EVERY:
            self._last_card_refresh = now
            for row in await self._repo.list_card_subscriptions():
                try:
                    subscription = await self._paddle.get_subscription(row["card_subscription_id"])
                    await self._apply_card_subscription(int(row["guild_id"]), subscription)
                except Exception:
                    logger.warning("Couldn't refresh card subscription for server %s.", row.get("guild_id"), exc_info=True)

    async def _discover_card_checkouts(self) -> None:
        """Card checkouts completed on the website: Paddle transactions with
        custom_data.guild_id that the bot hasn't credited yet."""
        assert self._paddle is not None
        since = self._card_discovery_since or self._clock() - _CARD_DISCOVERY_LOOKBACK
        transactions = await self._paddle.list_paid_transactions(updated_after=since.isoformat())
        for transaction in transactions:
            updated = parse_time(transaction.get("updated_at"))
            if updated is not None and updated > since:
                since = updated
            try:
                await self._credit_card_transaction(transaction)
            except Exception:
                logger.warning("Couldn't credit Paddle transaction %s.", transaction.get("id"), exc_info=True)
        self._card_discovery_since = since

    async def _credit_card_transaction(self, transaction: Dict[str, Any]) -> None:
        guild_id = _guild_id_of(transaction.get("custom_data"))
        subscription_id = transaction.get("subscription_id")
        if guild_id is None or not subscription_id:
            return  # not a PulseNotify server checkout
        if await self._repo.get_payment("paddle", transaction["id"]) is not None:
            return  # already credited (or started by an older bot version)
        row = await self._repo.get(guild_id)
        if row is not None and row.get("card_subscription_id") == subscription_id:
            return  # a monthly renewal — the subscription refresh handles those
        if not await self._repo.guild_exists(guild_id):
            logger.warning("Paddle transaction %s is for server %s, which the bot isn't in.", transaction["id"], guild_id)
            return

        subscription = await self._paddle.get_subscription(subscription_id)  # type: ignore[union-attr]
        await self._apply_card_subscription(guild_id, subscription)
        payment = await self._repo.create_payment(
            guild_id=guild_id,
            provider="paddle",
            external_id=transaction["id"],
            months=1,
            amount_usd=self.price_usd,
            requested_by=None,
            channel_id=None,
        )
        await self._complete(payment)

    async def _check_card_checkout(self, payment: Dict[str, Any]) -> None:
        if self._paddle is None:
            return
        transaction = await self._paddle.get_transaction(payment["external_id"])
        status = transaction.get("status")
        subscription_id = transaction.get("subscription_id")
        if status in ("paid", "completed") and subscription_id:
            subscription = await self._paddle.get_subscription(subscription_id)
            await self._apply_card_subscription(int(payment["guild_id"]), subscription)
            await self._complete(payment)
        elif status == "canceled":
            await self._repo.set_payment_status(payment["id"], "failed")
        elif self._expired(payment, _CARD_CHECKOUT_TTL):
            await self._repo.set_payment_status(payment["id"], "expired")

    async def _apply_card_subscription(self, guild_id: int, subscription: Dict[str, Any]) -> None:
        status = subscription.get("status")
        period_end = parse_time((subscription.get("current_billing_period") or {}).get("ends_at"))
        scheduled = subscription.get("scheduled_change") or {}
        cancels_at = parse_time(scheduled.get("effective_at")) if scheduled.get("action") == "cancel" else None

        row = await self._repo.get_or_create(guild_id, trial_ends_at=self._clock() + self._trial)
        changes: Dict[str, Any] = {
            "card_subscription_id": subscription.get("id"),
            "card_customer_id": subscription.get("customer_id"),
            "card_status": status,
            "card_cancels_at": cancels_at,
        }
        if status == "trialing":
            # Subscribed during the free trial: nothing has been charged, so
            # this isn't paid time — but Paddle's trial (whole days, rounded
            # up) may end a little after ours, so stretch ours to match.
            trial_ends_at = parse_time(row.get("trial_ends_at"))
            if period_end is not None and (trial_ends_at is None or period_end > trial_ends_at):
                changes["trial_ends_at"] = period_end
        else:
            current = parse_time(row.get("paid_until"))
            if status in _ACTIVE_CARD_STATUSES and period_end is not None and (current is None or period_end > current):
                changes["paid_until"] = period_end
        await self._repo.update(guild_id, changes)
        self._invalidate(guild_id)

    async def _check_crypto_payment(self, payment: Dict[str, Any]) -> None:
        if self._nowpayments is None:
            return
        remote = await self._nowpayments.get_payment(payment["external_id"])
        status = remote.get("payment_status")
        if status in PAID_STATUSES:
            problem = self._crypto_mismatch(payment, remote)
            if problem:
                logger.error("Crypto payment %s not credited: %s", payment["id"], problem)
                await self._repo.set_payment_status(payment["id"], "failed")
                return
            guild_id = int(payment["guild_id"])
            row = await self._repo.get_or_create(guild_id, trial_ends_at=self._clock() + self._trial)
            now = self._clock()
            start = max(now, parse_time(row.get("paid_until")) or now)
            await self._repo.update(
                guild_id, {"paid_until": start + datetime.timedelta(days=DAYS_PER_MONTH * int(payment["months"]))}
            )
            self._invalidate(guild_id)
            await self._complete(payment)
        elif status in FAILED_STATUSES:
            await self._repo.set_payment_status(payment["id"], "failed")
        elif status in EXPIRED_STATUSES or self._expired(payment, _CRYPTO_PAYMENT_TTL):
            await self._repo.set_payment_status(payment["id"], "expired")

    @staticmethod
    def _crypto_mismatch(payment: Dict[str, Any], remote: Dict[str, Any]) -> Optional[str]:
        """The website writes the pending row; NOWPayments is the truth. The
        payment must be for this server and at least the recorded amount."""
        order_id = str(remote.get("order_id") or "")
        if order_id and not order_id.startswith(f"{payment['guild_id']}-"):
            return f"order_id {order_id!r} is for another server"
        price = remote.get("price_amount")
        if price is not None and float(price) + 0.01 < float(payment["amount_usd"]):
            return f"paid ${price}, but the row says ${payment['amount_usd']}"
        return None

    async def _complete(self, payment: Dict[str, Any]) -> None:
        await self._repo.set_payment_status(payment["id"], "paid", paid_at=self._clock())
        logger.info(
            "Payment %s (%s, %s month(s)) received for server %s.",
            payment["id"],
            payment["provider"],
            payment["months"],
            payment["guild_id"],
        )
        if self.on_payment is not None:
            try:
                await self.on_payment(payment, await self.access(int(payment["guild_id"])))
            except Exception:
                logger.warning("Couldn't post the payment confirmation for %s.", payment["id"], exc_info=True)

    def _expired(self, payment: Dict[str, Any], ttl: datetime.timedelta) -> bool:
        created = parse_time(payment.get("created_at"))
        return created is not None and self._clock() - created > ttl


def _guild_id_of(custom_data: Any) -> Optional[int]:
    if not isinstance(custom_data, dict):
        return None
    value = str(custom_data.get("guild_id") or "").strip()
    return int(value) if value.isdigit() else None
