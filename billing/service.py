"""Per-server subscriptions: access checks, the bot-owner whitelist,
card payments through Polar, crypto payments made on the website, and
upgrade reminders.

Pricing: BILLING_PRICE_USD per month ($1.99 by default) — it must equal the
price of the Polar product. Card: `/notify subscribe` creates a hosted
Polar checkout for the server (the server ID travels as metadata.guild_id)
and `/notify cancel` opens Polar's customer portal. Crypto
(NOWPayments) is prepaid in 3/6/12-month blocks, paid on the website
(BILLING_SUBSCRIBE_URL).

Nothing is enforced while `enabled` is False (BILLING_ENABLED unset):
every server has access, no trial clock starts, and no billing rows are
written.

The bot learns about payments by polling, not webhooks, and always confirms
them with the provider itself:
- card: it lists Polar's subscriptions, picks up those whose metadata names
  a server it's in, and then refreshes each known subscription every few
  minutes (renewals, cancellations);
- crypto: the website creates the NOWPayments payment and inserts a
  `pending` row in notify_payments (guild_id, provider 'nowpayments',
  external_id = payment_id, months, amount_usd); the poller checks it with
  NOWPayments and credits the months once it's paid.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
import math
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from billing.access import Access, AccessReason, evaluate, parse_time, utcnow
from billing.links import sign_link
from billing.nowpayments import EXPIRED_STATUSES, FAILED_STATUSES, PAID_STATUSES, NowPaymentsClient, PaymentNotFound
from billing.polar import PolarClient, PolarError
from billing.repository import BillingRepository
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

DAYS_PER_MONTH = 30

# A crypto payment never sent stops being checked after this long (and any
# other pending row — only legacy ones exist — is closed after a day).
_STALE_PENDING_TTL = datetime.timedelta(hours=24)
_CRYPTO_PAYMENT_TTL = datetime.timedelta(days=3)
# A checkout still unpaid after this long gets one nudge DM, if it's no older than the window.
_ABANDONED_AFTER = datetime.timedelta(hours=1)
_ABANDONED_WINDOW = datetime.timedelta(hours=24)
_CARD_REFRESH_EVERY = datetime.timedelta(minutes=10)
_ACCESS_CACHE_TTL = datetime.timedelta(seconds=60)
# After the first full scan, look this far back for new card subscriptions.
_CARD_DISCOVERY_LOOKBACK = datetime.timedelta(days=3)
_ACTIVE_CARD_STATUSES = frozenset({"active", "past_due", "trialing"})
# A checkout link is reused for this long, so /notify subscribe pressed
# repeatedly (or by many admins) makes one Polar API call, not one each.
_CHECKOUT_CACHE_TTL = datetime.timedelta(minutes=10)
_MAX_ID_DIGITS = 19  # a Discord snowflake fits a bigint

PaymentCallback = Callable[[Dict[str, Any], Access], Awaitable[None]]


@dataclass(frozen=True)
class BillingEvent:
    """Something that happened to a server's subscription after it began:
    `card_renewed`, `card_failed` (charge declined, being retried),
    `card_cancel_scheduled` (ends at `until`), `card_ended`, `crypto_failed`
    (failed, refunded or underpaid) or `crypto_expired`. Worked out from
    what changed, so a restart can't repeat one."""

    kind: str
    guild_id: int
    until: Optional[datetime.datetime] = None
    payment: Optional[Dict[str, Any]] = None


EventCallback = Callable[[BillingEvent], Awaitable[None]]


class BillingUnavailable(Exception):
    """The requested payment method isn't configured on this bot."""


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
        price_usd: float = 1.99,
        trial_days: int = 7,
        grace_days: int = 3,
        subscribe_url: Optional[str] = None,
        link_secret: Optional[str] = None,
        link_ttl_minutes: int = 30,
        reminder_every_days: int = 7,
        reminder_max: int = 3,
        polar: Optional[PolarClient] = None,
        polar_product_id: Optional[str] = None,
        polar_success_url: Optional[str] = None,
        nowpayments: Optional[NowPaymentsClient] = None,
        clock: Callable[[], datetime.datetime] = utcnow,
    ) -> None:
        self._repo = repo
        self.enabled = enabled
        self.price_usd = round(price_usd, 2)
        self._trial = datetime.timedelta(days=trial_days)
        self._grace = datetime.timedelta(days=grace_days)
        self._subscribe_url = subscribe_url or None
        self._link_secret = link_secret or None
        self._link_ttl_seconds = link_ttl_minutes * 60
        self._reminder_every = datetime.timedelta(days=reminder_every_days)
        self._reminder_max = reminder_max
        self._polar = polar if polar_product_id else None
        self._polar_product_id = polar_product_id
        self._polar_success_url = polar_success_url or None
        self._nowpayments = nowpayments
        self._clock = clock
        self._cache: Dict[int, Tuple[datetime.datetime, Access]] = {}
        self._last_card_refresh: Optional[datetime.datetime] = None
        self._unknown_logged: Set[str] = set()
        self._full_scan_done = False
        self._checkout_cache: Dict[Tuple[int, int, int], Tuple[datetime.datetime, str]] = {}
        self._known_subscriptions: Set[str] = set()
        self.on_payment: Optional[PaymentCallback] = None
        self.on_event: Optional[EventCallback] = None

    @property
    def price_label(self) -> str:
        """"$1", "$2.50" — for every place the price is shown."""
        return format_usd(self.price_usd)

    @property
    def card_available(self) -> bool:
        return self._polar is not None

    @property
    def crypto_available(self) -> bool:
        return self._nowpayments is not None

    def subscribe_url(self, guild_id: int) -> Optional[str]:
        """The website's crypto payment page for this server (None until set)."""
        return with_server(self._subscribe_url, guild_id)

    @property
    def website_mode(self) -> bool:
        """Subscribing happens on the website: the subscribe page URL and the
        secret that signs the links to it are both set. The website then
        creates the Polar checkout (or the crypto payment) and shows the
        result; without this the bot creates the card checkout itself."""
        return self._subscribe_url is not None and self._link_secret is not None

    def plain_subscribe_url(self) -> Optional[str]:
        """The website's subscribe page exactly as configured — no token, so
        Discord's "Leaving Discord" prompt shows a short, clean link (None
        unless website_mode)."""
        return self._subscribe_url if self.website_mode else None  # type: ignore[return-value]

    async def website_subscribe_url(
        self,
        guild_id: int,
        *,
        user_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        guild_name: Optional[str] = None,
    ) -> Optional[str]:
        """The website's subscribe page for this server, with a signed,
        short-lived token saying which server, who asked and how many trial
        days are left (None unless website_mode)."""
        if not self.website_mode:
            return None
        token = sign_link(
            self._link_secret,  # type: ignore[arg-type]
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            guild_name=guild_name,
            trial_days=await self.remaining_trial_days(guild_id),
            ttl_seconds=self._link_ttl_seconds,
            now=self._clock().timestamp(),
        )
        parts = urlsplit(self._subscribe_url)  # type: ignore[arg-type]
        query = "&".join(part for part in (parts.query, urlencode({"t": token})) if part)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    async def aclose(self) -> None:
        for client in (self._polar, self._nowpayments):
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

    # ── card payments (Polar) ───────────────────────────────────────────

    async def start_card_checkout(
        self, guild_id: int, *, user_id: Optional[int] = None, channel_id: Optional[int] = None
    ) -> str:
        """A fresh hosted Polar checkout for this server. During the free
        trial the card is saved and the first charge is when the trial ends;
        afterwards it's charged straight away. `user_id`/`channel_id` (who
        ran /notify subscribe, and where) travel with the subscription,
        so the confirmation is posted back in that channel."""
        if self._polar is None or self._polar_product_id is None:
            raise BillingUnavailable("Card payments aren't available yet. Please check back soon.")
        now = self._clock()
        cache_key = (guild_id, user_id or 0, channel_id or 0)
        cached = self._checkout_cache.get(cache_key)
        if cached is not None and now - cached[0] < _CHECKOUT_CACHE_TTL:
            return cached[1]
        metadata = {"guild_id": str(guild_id)}
        if user_id:
            metadata["user_id"] = str(user_id)
        if channel_id:
            metadata["channel_id"] = str(channel_id)
        checkout = await self._polar.create_checkout(
            self._polar_product_id,
            metadata=metadata,
            trial_days=await self.remaining_trial_days(guild_id),
            success_url=self._polar_success_url,
        )
        url = checkout.get("url")
        if not _is_polar_url(url):
            raise BillingUnavailable("We couldn't create the payment link right now. Please try again in a moment.")
        self._checkout_cache[cache_key] = (now, url)
        return url

    async def manage_card_url(self, guild_id: int) -> str:
        """A pre-authenticated link to Polar's customer portal, where the
        card is updated or the subscription cancelled."""
        row = await self._repo.get(guild_id)
        if self._polar is None or row is None or not row.get("card_customer_id"):
            raise BillingUnavailable("This server doesn't have a card subscription to manage.")
        try:
            session = await self._polar.create_customer_session(row["card_customer_id"])
        except PermanentPlatformError as exc:
            # Polar doesn't know this customer — e.g. a subscription made in
            # its sandbox, or on another account. Nothing a member can fix.
            logger.warning("Polar refused a billing portal for server %s: %s", guild_id, exc)
            raise BillingUnavailable(
                "We couldn't open the billing page for this server's subscription. "
                "Please contact support and we'll sort it out."
            ) from exc
        url = session.get("customer_portal_url")
        if not _is_polar_url(url):
            raise BillingUnavailable("We couldn't open the billing page right now. Please try again in a moment.")
        return url

    async def can_manage_card(self, guild_id: int, user_id: int, *, server_owner_id: Optional[int]) -> bool:
        """Whether this person may open the server's card billing portal.

        The portal link is pre-authenticated as the Polar customer — the
        person who paid — and shows their email and billing details, so it
        isn't handed to every server admin: only the person who subscribed
        (recorded when the subscription was credited) or the server owner."""
        if server_owner_id is not None and user_id == server_owner_id:
            return True
        row = await self._repo.get(guild_id)
        subscription_id = (row or {}).get("card_subscription_id")
        if not subscription_id:
            return False
        payment = await self._repo.get_payment("polar", subscription_id)
        return payment is not None and payment.get("requested_by") == user_id

    async def card_payer_id(self, guild_id: int) -> Optional[int]:
        row = await self._repo.get(guild_id)
        subscription_id = (row or {}).get("card_subscription_id")
        if not subscription_id:
            return None
        payment = await self._repo.get_payment("polar", subscription_id)
        return payment.get("requested_by") if payment else None

    async def remaining_trial_days(self, guild_id: int) -> int:
        """Whole days left in the server's free trial (rounded up), so a card
        subscription started during the trial isn't charged until it ends."""
        row = await self._repo.get(guild_id)
        trial_ends_at = parse_time((row or {}).get("trial_ends_at"))
        if trial_ends_at is None:
            return 0
        remaining = (trial_ends_at - self._clock()).total_seconds()
        return max(0, math.ceil(remaining / 86400))

    async def check_setup(self) -> None:
        """Startup log lines about the payment setup. Never raises."""
        if not self.enabled:
            return
        if self._polar is None:
            logger.warning("POLAR_ACCESS_TOKEN / POLAR_PRODUCT_ID not set — card payments are off.")
        else:
            try:
                product = await self._polar.get_product(self._polar_product_id)  # type: ignore[arg-type]
            except PolarError as exc:
                logger.warning(
                    "Polar refused the product check (fine if the token lacks products:read, but checkouts "
                    "need checkouts:write): %s",
                    exc,
                )
            except Exception as exc:
                logger.warning("Couldn't check the Polar product: %s", exc)
            else:
                self._check_product(product)
        if self._nowpayments is None:
            logger.info("NOWPAYMENTS_API_KEY not set — crypto payments are off.")

    def _check_product(self, product: Dict[str, Any]) -> None:
        if product.get("is_archived"):
            logger.warning("The Polar product is archived — new checkouts will fail.")
        if product.get("is_recurring") is False:
            logger.warning("The Polar product isn't a subscription — create a monthly recurring product.")
        amounts = {
            price.get("price_amount")
            for price in product.get("prices") or []
            if isinstance(price, dict) and price.get("price_amount") is not None
        }
        expected = round(self.price_usd * 100)
        if amounts and expected not in amounts:
            logger.warning(
                "The Polar product costs %s cents but BILLING_PRICE_USD says %s — they must match.",
                sorted(amounts),
                expected,
            )

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

    async def abandoned_checkouts(self) -> List[Dict[str, Any]]:
        """Pending payments still unpaid `_ABANDONED_AFTER` after they started
        (but not older than `_ABANDONED_WINDOW`, so a backlog is never DMed
        in one go). The caller dedupes against the DM log."""
        if not self.enabled:
            return []
        now = self._clock()
        found = []
        for payment in await self._repo.list_pending():
            created = parse_time(payment.get("created_at"))
            if created is None or not payment.get("requested_by"):
                continue
            if _ABANDONED_AFTER <= now - created <= _ABANDONED_WINDOW:
                found.append(payment)
        return found

    # ── poller ──────────────────────────────────────────────────────────

    async def poll_once(self) -> None:
        for payment in await self._repo.list_pending():
            try:
                if payment["provider"] == "nowpayments":
                    await self._check_crypto_payment(payment)
                elif self._expired(payment, _STALE_PENDING_TTL):
                    await self._repo.set_payment_status(payment["id"], "expired")
            except Exception:
                logger.warning("Couldn't check payment %s.", payment.get("id"), exc_info=True)

        if self._polar is None:
            return
        try:
            await self._discover_card_subscriptions()
        except Exception:
            logger.warning("Couldn't look for new card subscriptions.", exc_info=True)

        now = self._clock()
        if self._last_card_refresh is None or now - self._last_card_refresh >= _CARD_REFRESH_EVERY:
            self._last_card_refresh = now
            for row in await self._repo.list_card_subscriptions():
                try:
                    subscription = await self._polar.get_subscription(row["card_subscription_id"])
                    await self._apply_card_subscription(int(row["guild_id"]), subscription)
                except Exception:
                    logger.warning("Couldn't refresh card subscription for server %s.", row.get("guild_id"), exc_info=True)

    async def _discover_card_subscriptions(self) -> None:
        """New card subscriptions: Polar subscriptions whose metadata names a
        server the bot is in and that it hasn't credited yet. The first scan
        looks at everything (covers the bot being offline for a while), then
        only the last few days."""
        assert self._polar is not None
        since = self._clock() - _CARD_DISCOVERY_LOOKBACK if self._full_scan_done else None
        subscriptions = await self._polar.list_subscriptions(started_after=since, statuses=sorted(_ACTIVE_CARD_STATUSES))
        self._full_scan_done = True
        for subscription in subscriptions:
            if subscription.get("id") in self._known_subscriptions:
                continue
            try:
                await self._credit_card_subscription(subscription)
            except Exception:
                logger.warning("Couldn't credit Polar subscription %s.", subscription.get("id"), exc_info=True)

    async def _credit_card_subscription(self, subscription: Dict[str, Any]) -> None:
        subscription_id = subscription.get("id")
        guild_id = _guild_id_of(subscription.get("metadata"))
        if not subscription_id:
            return
        if guild_id is None:
            self._known_subscriptions.add(subscription_id)  # not a DiscoNotify server checkout
            return
        row = await self._repo.get(guild_id)
        if row is not None and row.get("card_subscription_id") == subscription_id:
            self._known_subscriptions.add(subscription_id)  # already applied — refreshes keep it current
            return
        if await self._repo.get_payment("polar", subscription_id) is not None:
            self._known_subscriptions.add(subscription_id)
            return
        if not await self._repo.guild_exists(guild_id):
            logger.warning("Polar subscription %s is for server %s, which the bot isn't in.", subscription_id, guild_id)
            self._known_subscriptions.add(subscription_id)
            return
        if row is not None and row.get("card_subscription_id") and row.get("card_status") in _ACTIVE_CARD_STATUSES:
            logger.warning(
                "Server %s now has two card subscriptions (%s and %s) — cancel one in the Polar dashboard.",
                guild_id,
                row["card_subscription_id"],
                subscription_id,
            )

        await self._apply_card_subscription(guild_id, subscription)
        payment = await self._repo.create_payment(
            guild_id=guild_id,
            provider="polar",
            external_id=subscription_id,
            months=1,
            amount_usd=self.price_usd,
            requested_by=_int_of(subscription.get("metadata"), "user_id"),
            channel_id=_int_of(subscription.get("metadata"), "channel_id"),
        )
        self._known_subscriptions.add(subscription_id)
        await self._complete(payment)

    async def _apply_card_subscription(self, guild_id: int, subscription: Dict[str, Any]) -> None:
        status = subscription.get("status")
        period_end = parse_time(subscription.get("current_period_end"))
        cancels_at = None
        if subscription.get("cancel_at_period_end"):
            cancels_at = parse_time(subscription.get("ends_at")) or period_end

        row = await self._repo.get_or_create(guild_id, trial_ends_at=self._clock() + self._trial)
        before = dict(row)
        changes: Dict[str, Any] = {
            "card_subscription_id": subscription.get("id"),
            "card_customer_id": subscription.get("customer_id"),
            "card_status": status,
            "card_cancels_at": cancels_at,
        }
        if status == "trialing":
            # Subscribed during the free trial: nothing has been charged, so
            # this isn't paid time. The trial Polar runs may end a little
            # after ours (whole days, rounded up), so stretch ours to match.
            trial_end = parse_time(subscription.get("trial_end")) or period_end
            trial_ends_at = parse_time(row.get("trial_ends_at"))
            if trial_end is not None and (trial_ends_at is None or trial_end > trial_ends_at):
                changes["trial_ends_at"] = trial_end
        else:
            current = parse_time(row.get("paid_until"))
            ended_at = parse_time(subscription.get("ended_at"))
            if status in _ACTIVE_CARD_STATUSES and period_end is not None and (current is None or period_end > current):
                changes["paid_until"] = period_end
            elif status == "canceled" and ended_at is not None and period_end is not None and ended_at < period_end:
                # Ended before the paid period did (a refund, a chargeback, or
                # Polar revoking it): the rest of that period is no longer
                # paid for. Only when the paid time is this subscription's own,
                # so prepaid crypto time isn't cut.
                if current is not None and current == period_end:
                    changes["paid_until"] = ended_at
        await self._repo.update(guild_id, changes)
        self._invalidate(guild_id)
        if before.get("card_subscription_id") == subscription.get("id"):
            for kind, until in self._card_transitions(before, changes, cancels_at):
                await self._emit(BillingEvent(kind, guild_id, until=until))

    @staticmethod
    def _card_transitions(
        before: Dict[str, Any], changes: Dict[str, Any], cancels_at: Optional[datetime.datetime]
    ) -> "List[Tuple[str, Optional[datetime.datetime]]]":
        """What changed for a subscription we already knew: a month was paid,
        a charge failed, it was cancelled, or it ended."""
        found: "List[Tuple[str, Optional[datetime.datetime]]]" = []
        old_status, new_status = before.get("card_status"), changes["card_status"]
        old_paid = parse_time(before.get("paid_until"))
        new_paid = changes.get("paid_until")
        if new_paid is not None and (old_paid is None or new_paid > old_paid) and new_status == "active":
            found.append(("card_renewed", new_paid))
        if new_status == "past_due" and old_status != "past_due":
            found.append(("card_failed", None))
        if cancels_at is not None and parse_time(before.get("card_cancels_at")) is None and new_status in _ACTIVE_CARD_STATUSES:
            found.append(("card_cancel_scheduled", cancels_at))
        if new_status == "canceled" and old_status in _ACTIVE_CARD_STATUSES:
            found.append(("card_ended", parse_time(changes.get("paid_until")) or old_paid))
        return found

    async def _emit(self, event: BillingEvent) -> None:
        logger.info("Billing event %s for server %s.", event.kind, event.guild_id)
        if self.on_event is None:
            return
        try:
            await self.on_event(event)
        except Exception:
            logger.warning("Couldn't deliver the %s alert for server %s.", event.kind, event.guild_id, exc_info=True)

    async def _check_crypto_payment(self, payment: Dict[str, Any]) -> None:
        if self._nowpayments is None:
            return
        try:
            remote = await self._nowpayments.get_payment(payment["external_id"])
        except PaymentNotFound:
            await self._unknown_crypto_payment(payment)
            return
        status = remote.get("payment_status")
        if status in PAID_STATUSES:
            problem = self._crypto_mismatch(payment, remote)
            if problem:
                logger.error("Crypto payment %s not credited: %s", payment["id"], problem)
                await self._repo.set_payment_status(payment["id"], "failed")
                await self._emit(BillingEvent("crypto_failed", int(payment["guild_id"]), payment=payment))
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
            await self._emit(BillingEvent("crypto_failed", int(payment["guild_id"]), payment=payment))
        elif status in EXPIRED_STATUSES or self._expired(payment, _CRYPTO_PAYMENT_TTL):
            await self._repo.set_payment_status(payment["id"], "expired")
            await self._emit(BillingEvent("crypto_expired", int(payment["guild_id"]), payment=payment))

    async def _unknown_crypto_payment(self, payment: Dict[str, Any]) -> None:
        """NOWPayments doesn't know this payment: made under another API key
        or in the other (sandbox / live) mode, or not created there at all.
        Nothing can be learned about it, so it's quietly closed once it's past
        the normal payment window (no alert to the server) — until then it's
        logged once, not on every poll."""
        key = str(payment["id"])
        if self._expired(payment, _CRYPTO_PAYMENT_TTL):
            logger.info("Closing payment %s: NOWPayments has no record of it.", key)
            await self._repo.set_payment_status(payment["id"], "expired")
            self._unknown_logged.discard(key)
        elif key not in self._unknown_logged:
            self._unknown_logged.add(key)
            logger.warning(
                "NOWPayments has no payment %s (id %s) — is NOWPAYMENTS_API_KEY / NOWPAYMENTS_SANDBOX the "
                "ones it was made with? It will be closed once it's %d days old.",
                key,
                payment.get("external_id"),
                _CRYPTO_PAYMENT_TTL.days,
            )

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


def _int_of(metadata: Any, key: str) -> Optional[int]:
    if not isinstance(metadata, dict):
        return None
    value = str(metadata.get(key) or "").strip()
    return int(value) if value.isdigit() and len(value) <= _MAX_ID_DIGITS else None


def _is_polar_url(url: Any) -> bool:
    """Only https links on Polar's own domain are handed to users, whatever
    the API response says."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "polar.sh" or host.endswith(".polar.sh"))


def _guild_id_of(metadata: Any) -> Optional[int]:
    return _int_of(metadata, "guild_id")
