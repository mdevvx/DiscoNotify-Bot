"""Per-server subscriptions: access checks, starting checkouts, the bot-owner
whitelist, and the payment poller.

Pricing: BILLING_PRICE_USD per month ($2.99 by default). Card (Paddle)
renews monthly by itself; crypto
(NOWPayments) can't auto-debit, so it's prepaid in 3/6/12-month blocks —
that also clears the coins' minimum payment sizes (~$2–5).

Nothing is enforced while `enabled` is False (BILLING_ENABLED unset):
every server has access, no trial clock starts, and no billing rows are
written — so the bot keeps working exactly as before until the operator's
Paddle/NOWPayments accounts are ready.

Payments are confirmed by polling, not webhooks (the bot has no public
URL): `poll_once()` checks every pending checkout and, every few minutes,
every card subscription, then calls `on_payment` so the bot can post a
confirmation in the channel the payment was started from.
"""

from __future__ import annotations

import datetime
import math
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from billing.access import Access, AccessReason, evaluate, parse_time, utcnow
from billing.nowpayments import EXPIRED_STATUSES, FAILED_STATUSES, PAID_STATUSES, NowPaymentsClient
from billing.paddle import PaddleClient
from billing.repository import BillingRepository
from utils.logger import get_logger

logger = get_logger(__name__)

DAYS_PER_MONTH = 30
CRYPTO_PLANS: Tuple[int, ...] = (3, 6, 12)
# NOWPayments currency codes → what the server admin sees, lowest minimum
# payment first. Minimums move with network fees and are checked live
# before every payment (USDT on TRON and BTC can need $10–20+).
CRYPTO_COINS: Dict[str, str] = {
    "usdtbsc": "USDT (BNB Chain · BEP20)",
    "ltc": "Litecoin (LTC)",
    "sol": "Solana (SOL)",
    "eth": "Ethereum (ETH)",
    "usdttrc20": "USDT (TRON · TRC20)",
    "btc": "Bitcoin (BTC)",
}
# NOWPayments compares the minimum against the crypto amount after fees,
# so the plan must clear it with a little room.
_MINIMUM_MARGIN = 1.05

# A card checkout nobody completed, or a crypto payment never sent, stops
# being checked after this long.
_CARD_CHECKOUT_TTL = datetime.timedelta(hours=24)
_CRYPTO_PAYMENT_TTL = datetime.timedelta(days=3)
_CARD_REFRESH_EVERY = datetime.timedelta(minutes=10)
_ACCESS_CACHE_TTL = datetime.timedelta(seconds=60)
_ACTIVE_CARD_STATUSES = frozenset({"active", "past_due", "trialing"})

PaymentCallback = Callable[[Dict[str, Any], Access], Awaitable[None]]


def format_usd(amount: float) -> str:
    """$1, $3, $2.50 — whole dollars without cents."""
    return f"${amount:.0f}" if float(amount).is_integer() else f"${amount:.2f}"


class BillingUnavailable(Exception):
    """The requested payment method isn't configured on this bot."""


class BillingService:
    def __init__(
        self,
        repo: BillingRepository,
        *,
        enabled: bool,
        price_usd: float = 2.99,
        trial_days: int = 7,
        grace_days: int = 3,
        paddle: Optional[PaddleClient] = None,
        paddle_price_id: Optional[str] = None,
        nowpayments: Optional[NowPaymentsClient] = None,
        clock: Callable[[], datetime.datetime] = utcnow,
    ) -> None:
        self._repo = repo
        self.enabled = enabled
        self.price_usd = round(price_usd, 2)
        self._trial = datetime.timedelta(days=trial_days)
        self._grace = datetime.timedelta(days=grace_days)
        self._paddle = paddle if paddle_price_id else None
        self._paddle_price_id = paddle_price_id
        self._nowpayments = nowpayments
        self._clock = clock
        self._cache: Dict[int, Tuple[datetime.datetime, Access]] = {}
        self._last_card_refresh: Optional[datetime.datetime] = None
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

    # ── starting payments ───────────────────────────────────────────────

    async def start_card_checkout(self, guild_id: int, *, user_id: int, channel_id: Optional[int]) -> str:
        if self._paddle is None or self._paddle_price_id is None:
            raise BillingUnavailable("Card payments aren't set up on this bot yet.")
        transaction = await self._paddle.create_transaction(
            self._paddle_price_id,
            {"guild_id": str(guild_id), "requested_by": str(user_id)},
            trial_days=await self.remaining_trial_days(guild_id),
            amount_cents=round(self.price_usd * 100),
        )
        url = (transaction.get("checkout") or {}).get("url")
        if not url:
            raise BillingUnavailable(
                "Paddle didn't return a checkout link — the default payment link isn't set up (see README)."
            )
        await self._repo.create_payment(
            guild_id=guild_id,
            provider="paddle",
            external_id=transaction["id"],
            months=1,
            amount_usd=self.price_usd,
            requested_by=user_id,
            channel_id=channel_id,
            checkout_url=url,
        )
        return url

    async def start_crypto_payment(
        self, guild_id: int, *, months: int, coin: str, user_id: int, channel_id: Optional[int]
    ) -> Dict[str, Any]:
        if self._nowpayments is None:
            raise BillingUnavailable("Crypto payments aren't set up on this bot yet.")
        if months not in CRYPTO_PLANS or coin not in CRYPTO_COINS:
            raise ValueError("Unsupported plan or coin.")
        amount = round(months * self.price_usd, 2)
        minimum = await self._nowpayments.min_amount_usd(coin) * _MINIMUM_MARGIN
        if amount < minimum:
            raise BillingUnavailable(self._below_minimum_message(coin, minimum))
        payment = await self._nowpayments.create_payment(
            price_usd=amount,
            pay_currency=coin,
            order_id=f"{guild_id}-{uuid.uuid4().hex[:12]}",
            description=f"PulseNotify — {months} months for server {guild_id}",
        )
        await self._repo.create_payment(
            guild_id=guild_id,
            provider="nowpayments",
            external_id=str(payment["payment_id"]),
            months=months,
            amount_usd=amount,
            requested_by=user_id,
            channel_id=channel_id,
            metadata={
                "pay_currency": payment.get("pay_currency"),
                "pay_amount": payment.get("pay_amount"),
                "pay_address": payment.get("pay_address"),
            },
        )
        return payment

    def _below_minimum_message(self, coin: str, minimum: float) -> str:
        label = CRYPTO_COINS[coin]
        longer = [plan for plan in CRYPTO_PLANS if plan * self.price_usd >= minimum]
        options = []
        if longer:
            options.append(f"choose the {longer[0]}-month plan")
        options.append("pay with USDT (BNB Chain) or Litecoin, which have much lower minimums")
        return f"{label} payments need at least {format_usd(round(minimum, 2))} right now — " + ", or ".join(options) + "."

    async def remaining_trial_days(self, guild_id: int) -> int:
        """Whole days left in the server's free trial (rounded up), so a card
        subscription started during the trial isn't charged until it ends."""
        row = await self._repo.get(guild_id)
        trial_ends_at = parse_time((row or {}).get("trial_ends_at"))
        if trial_ends_at is None:
            return 0
        remaining = (trial_ends_at - self._clock()).total_seconds()
        return max(0, math.ceil(remaining / 86400))

    async def manage_card_url(self, guild_id: int) -> str:
        row = await self._repo.get(guild_id)
        if self._paddle is None or row is None or not row.get("card_customer_id") or not row.get("card_subscription_id"):
            raise BillingUnavailable("This server has no card subscription to manage.")
        session = await self._paddle.create_portal_session(row["card_customer_id"], [row["card_subscription_id"]])
        urls = session.get("urls") or {}
        subscriptions = urls.get("subscriptions") or [{}]
        url = (urls.get("general") or {}).get("overview") or subscriptions[0].get("cancel_subscription")
        if not url:
            raise BillingUnavailable("Paddle didn't return a customer portal link.")
        return url

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

        now = self._clock()
        if self._paddle is not None and (
            self._last_card_refresh is None or now - self._last_card_refresh >= _CARD_REFRESH_EVERY
        ):
            self._last_card_refresh = now
            for row in await self._repo.list_card_subscriptions():
                try:
                    subscription = await self._paddle.get_subscription(row["card_subscription_id"])
                    await self._apply_card_subscription(int(row["guild_id"]), subscription)
                except Exception:
                    logger.warning("Couldn't refresh card subscription for server %s.", row.get("guild_id"), exc_info=True)

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
