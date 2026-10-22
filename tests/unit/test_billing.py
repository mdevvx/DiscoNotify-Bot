"""Billing: access rules, the trial, card subscriptions (Polar) including
cancellation, refunds and the security rules around checkout and portal
links, crypto prepayments made on the website (NOWPayments), the whitelist,
upgrade reminders and the payment poller — driven through fake provider
clients and the fake database, with an injectable clock."""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from billing.access import Access, AccessReason, describe_access, evaluate
from billing.nowpayments import NowPaymentsClient
from billing.polar import PolarClient, PolarError
from billing.repository import BillingRepository
from billing.service import BillingService, BillingUnavailable, format_usd, with_server
from tests.unit.fakes import FakeDatabase
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

_GUILD = 1
_START = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
_GRACE = datetime.timedelta(days=3)


class Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += datetime.timedelta(**kwargs)


class FakePolar:
    """Polar as the bot sees it: the subscriptions that exist (created by
    checkouts the bot started, each carrying metadata.guild_id)."""

    def __init__(self) -> None:
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.checkouts: List[Dict[str, Any]] = []
        self.sessions: List[str] = []
        self.listed: List[Any] = []
        self.checkout_url: Optional[str] = None
        self.portal_url: Optional[str] = None
        self.list_fails = False
        self.product: Dict[str, Any] = {"prices": [{"price_amount": 299}], "is_recurring": True, "is_archived": False}

    async def create_checkout(self, product_id: str, *, metadata: Dict[str, str], trial_days: int = 0, success_url: Optional[str] = None):
        self.checkouts.append({"product_id": product_id, "metadata": metadata, "trial_days": trial_days, "success_url": success_url})
        return {"id": f"co_{len(self.checkouts)}", "url": self.checkout_url if self.checkout_url is not None else f"https://buy.polar.sh/co_{len(self.checkouts)}"}

    async def list_subscriptions(self, *, started_after: Any = None, statuses: Any = None):
        self.listed.append(started_after)
        if self.list_fails:
            raise TransientPlatformError("Polar error 503")
        return [sub for sub in self.subscriptions.values() if statuses is None or sub["status"] in statuses]

    async def get_subscription(self, subscription_id: str):
        return self.subscriptions[subscription_id]

    async def create_customer_session(self, customer_id: str, *, return_url: Optional[str] = None):
        self.sessions.append(customer_id)
        return {"customer_portal_url": self.portal_url if self.portal_url is not None else f"https://polar.sh/pulsenotify/portal?customer_session_token=t_{customer_id}"}

    async def get_product(self, product_id: str):
        return self.product

    async def aclose(self) -> None:
        pass

    def subscribe(
        self,
        sub_id: str = "sub_1",
        *,
        status: str,
        period_end: datetime.datetime,
        guild_id: Any = _GUILD,
        user_id: int = 7,
        channel_id: int = 555,
        trial_end: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        """A customer completing a checkout (or a subscription changing)."""
        metadata = {} if guild_id is None else {"guild_id": str(guild_id), "user_id": str(user_id), "channel_id": str(channel_id)}
        subscription = {
            "id": sub_id,
            "status": status,
            "customer_id": "cus_1",
            "current_period_end": period_end.isoformat(),
            "trial_end": trial_end.isoformat() if trial_end else None,
            "cancel_at_period_end": False,
            "ends_at": None,
            "ended_at": None,
            "metadata": metadata,
        }
        self.subscriptions[sub_id] = subscription
        return subscription


class FakeNowPayments:
    def __init__(self) -> None:
        self.payments: Dict[str, Dict[str, Any]] = {}

    async def get_payment(self, payment_id: str):
        return self.payments[payment_id]

    async def aclose(self) -> None:
        pass

    def create(self, payment_id: str, *, price_usd: float, order_id: str, status: str = "waiting") -> None:
        self.payments[payment_id] = {
            "payment_id": payment_id,
            "payment_status": status,
            "price_amount": price_usd,
            "order_id": order_id,
        }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    database.tables["notify_guilds"].rows.append({"guild_id": _GUILD, "enabled": True})
    return database


def _service(db: FakeDatabase, clock: Clock, **kwargs: Any) -> BillingService:
    defaults: Dict[str, Any] = {"enabled": True, "polar": FakePolar(), "polar_product_id": "prod_1", "nowpayments": FakeNowPayments()}
    defaults.update(kwargs)
    return BillingService(BillingRepository(db), clock=clock, **defaults)  # type: ignore[arg-type]


def _row(db: FakeDatabase) -> Dict[str, Any]:
    return db.tables["notify_billing"].rows[0]


async def _activated(service: BillingService, db: FakeDatabase, clock: Clock, days: int = 7) -> None:
    """What the website's notify_activate_trial() does: the trial starts now."""
    await service.access(_GUILD)
    _row(db)["trial_ends_at"] = (clock.now + datetime.timedelta(days=days)).isoformat()
    _row(db)["trial_activated_at"] = clock.now.isoformat()
    service._invalidate(_GUILD)


def _website_row(
    db: FakeDatabase, provider: str, external_id: str, *, months: int, amount_usd: float, created_at: datetime.datetime = _START
) -> None:
    """What the website inserts after creating a payment."""
    db.tables["notify_payments"].rows.append(
        {
            "id": f"row-{external_id}",
            "guild_id": _GUILD,
            "provider": provider,
            "external_id": external_id,
            "months": months,
            "amount_usd": amount_usd,
            "status": "pending",
            "requested_by": None,
            "channel_id": None,
            "created_at": created_at.isoformat(),
        }
    )


# ── access rules ────────────────────────────────────────────────────────


def test_whitelist_beats_everything() -> None:
    access = evaluate({"whitelisted": True, "trial_ends_at": _START.isoformat()}, now=_START, grace=_GRACE)
    assert access == Access(True, AccessReason.WHITELISTED)


def test_paid_time_counts_before_the_trial() -> None:
    row = {"trial_ends_at": (_START + datetime.timedelta(days=2)).isoformat(), "paid_until": (_START + datetime.timedelta(days=20)).isoformat()}
    assert evaluate(row, now=_START, grace=_GRACE).reason == AccessReason.PAID


def test_grace_period_after_paid_time_then_expired() -> None:
    row = {"trial_ends_at": _START.isoformat(), "paid_until": _START.isoformat()}
    assert evaluate(row, now=_START + datetime.timedelta(days=2), grace=_GRACE).reason == AccessReason.GRACE
    assert evaluate(row, now=_START + datetime.timedelta(days=4), grace=_GRACE).allowed is False


# ── trial, whitelist, billing off ───────────────────────────────────────


async def test_billing_off_gives_everyone_access_and_writes_nothing(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock, enabled=False)

    access = await service.access(_GUILD)

    assert access.allowed and access.reason == AccessReason.BILLING_OFF
    assert await service.allowed_guild_ids({_GUILD, 2}) == {_GUILD, 2}
    assert db.tables["notify_billing"].rows == []


async def test_a_new_server_has_no_trial_until_it_is_activated(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)

    first = await service.access(_GUILD)

    assert first.allowed is False and first.reason == AccessReason.NEEDS_ACTIVATION
    assert _row(db)["activation_required"] is True and _row(db).get("trial_ends_at") is None
    assert await service.allowed_guild_ids({_GUILD}) == set()
    assert await service.remaining_trial_days(_GUILD) == 0


async def test_an_activated_server_gets_the_trial_then_loses_access(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await _activated(service, db, clock)

    first = await service.access(_GUILD)
    clock.advance(days=8)
    later = await service.allowed_guild_ids({_GUILD})

    assert first.reason == AccessReason.TRIAL and first.until == _START + datetime.timedelta(days=7)
    assert later == set()


async def test_an_existing_server_keeps_its_trial_without_activating(db: FakeDatabase, clock: Clock) -> None:
    """Rows from before activation existed have a trial and activation_required false."""
    db.tables["notify_billing"].rows.append(
        {"guild_id": _GUILD, "trial_ends_at": (_START + datetime.timedelta(days=3)).isoformat(), "activation_required": False}
    )

    access = await _service(db, clock).access(_GUILD)

    assert access.allowed and access.reason == AccessReason.TRIAL


async def test_paying_works_without_activating_the_trial(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    _row(db)["paid_until"] = (_START + datetime.timedelta(days=30)).isoformat()
    service._invalidate(_GUILD)

    assert (await service.access(_GUILD)).reason == AccessReason.PAID


async def test_the_inviter_is_remembered_and_unactivated_servers_listed(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)

    await service.set_invited_by(_GUILD, 77)

    assert _row(db)["invited_by"] == 77
    assert [row["guild_id"] for row in await service.awaiting_activation()] == [_GUILD]
    await _activated(service, db, clock)
    assert await service.awaiting_activation() == []


def test_the_activation_link_defaults_to_the_websites_activate_page(db: FakeDatabase, clock: Clock) -> None:
    assert _service(db, clock, subscribe_url="https://disconotify.com/subscribe").activate_url == "https://disconotify.com/activate"
    assert _service(db, clock).activate_url == "https://disconotify.com/activate"
    assert _service(db, clock, activate_url="https://x.test/go").activate_url == "https://x.test/go"
    assert _service(db, clock, subscribe_url="https://disconotify.com/subscribe?ref=bot").site_url("/manage") == "https://disconotify.com/manage"


async def test_upcoming_card_charges_cover_renewals_and_trial_conversions_but_not_cancelled_ones(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    soon = _START + datetime.timedelta(days=2)
    row = _row(db)
    row.update({"card_subscription_id": "sub_1", "card_status": "active", "paid_until": soon.isoformat()})

    window = datetime.timedelta(days=3)
    assert await service.upcoming_card_charges(window) == [{"guild_id": _GUILD, "charge_at": soon}]
    assert await service.upcoming_card_charges(datetime.timedelta(days=1)) == []

    row.update({"card_status": "trialing", "trial_ends_at": soon.isoformat()})
    assert [c["charge_at"] for c in await service.upcoming_card_charges(window)] == [soon]

    row["card_cancels_at"] = soon.isoformat()
    assert await service.upcoming_card_charges(window) == []


async def test_whitelisted_server_never_needs_to_pay(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    clock.advance(days=30)

    await service.whitelist(_GUILD, by=99, note="my own server")
    allowed = await service.access(_GUILD)
    await service.unwhitelist(_GUILD)
    removed = await service.access(_GUILD)

    assert allowed.reason == AccessReason.WHITELISTED
    assert removed.allowed is False
    assert _row(db)["whitelist_note"] == "my own server"


# ── card (Polar) ────────────────────────────────────────────────────────


async def test_a_paid_card_subscription_is_found_and_grants_access_until_the_period_ends(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    announced: List[Any] = []

    async def on_payment(payment: Dict[str, Any], access: Access) -> None:
        announced.append((payment["provider"], payment["requested_by"], payment["channel_id"], access.reason))

    service.on_payment = on_payment
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    polar.subscribe(status="active", period_end=period_end)

    await service.poll_once()
    await service.poll_once()  # already credited — no second confirmation

    access = await service.access(_GUILD)
    assert access.reason == AccessReason.PAID and access.until == period_end
    payments = db.tables["notify_payments"].rows
    assert [(p["provider"], p["external_id"], p["status"]) for p in payments] == [("polar", "sub_1", "paid")]
    assert announced == [("polar", 7, 555, AccessReason.PAID)]  # confirmation goes back to the channel it was started from
    assert "renews automatically by card" in describe_access(access, _row(db))


async def test_subscriptions_without_a_server_id_or_for_unknown_servers_are_ignored(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    end = clock.now + datetime.timedelta(days=30)
    polar.subscribe("sub_1", status="active", period_end=end, guild_id=None)  # e.g. a checkout link made in the dashboard
    polar.subscribe("sub_2", status="active", period_end=end, guild_id=999)  # a server the bot isn't in
    polar.subscribe("sub_3", status="active", period_end=end, guild_id="9" * 40)  # not a plausible server ID

    await service.poll_once()

    assert db.tables["notify_payments"].rows == []
    assert db.tables["notify_billing"].rows == []


async def test_the_first_scan_reads_everything_then_only_the_last_few_days(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)

    await service.poll_once()
    await service.poll_once()

    assert polar.listed == [None, _START - datetime.timedelta(days=3)]


async def test_cancelling_during_the_trial_is_never_charged(db: FakeDatabase, clock: Clock) -> None:
    """Card saved during the trial, then cancelled before it ends: Polar
    ends it at the trial end and nothing was ever charged — access simply
    ends with the trial."""
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    trial_end = _START + datetime.timedelta(days=7)
    subscription = polar.subscribe(status="trialing", period_end=trial_end, trial_end=trial_end)
    await service.poll_once()

    during = await service.access(_GUILD)
    assert during.reason == AccessReason.TRIAL
    assert _row(db).get("paid_until") is None  # trialing isn't paid time
    assert "card subscription starts" in describe_access(during, _row(db))

    subscription.update(cancel_at_period_end=True, ends_at=trial_end.isoformat())  # customer cancels in the portal
    clock.advance(minutes=11)
    await service.poll_once()
    assert "Free trial — ends" in describe_access(await service.access(_GUILD), _row(db))

    clock.now = trial_end + datetime.timedelta(minutes=1)
    subscription.update(status="canceled", ended_at=trial_end.isoformat())
    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()
    assert _row(db)["card_status"] == "canceled"


async def test_cancelling_after_paying_keeps_access_until_the_paid_month_ends(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    subscription = polar.subscribe(status="active", period_end=period_end)
    await service.poll_once()

    subscription.update(cancel_at_period_end=True, ends_at=period_end.isoformat())  # cancels, no renewal
    clock.advance(minutes=11)
    await service.poll_once()
    still_paid = await service.access(_GUILD)

    assert still_paid.reason == AccessReason.PAID and still_paid.until == period_end
    assert "stops unless renewed" in describe_access(still_paid, _row(db))

    clock.now = period_end + datetime.timedelta(days=4)  # past the grace period
    subscription.update(status="canceled", ended_at=period_end.isoformat())
    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()


async def test_monthly_renewal_extends_access_without_a_new_confirmation(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    announced: List[str] = []

    async def on_payment(payment: Dict[str, Any], access: Access) -> None:
        announced.append(payment["external_id"])

    service.on_payment = on_payment
    await service.access(_GUILD)
    clock.advance(days=8)
    first_end = clock.now + datetime.timedelta(days=30)
    subscription = polar.subscribe(status="active", period_end=first_end)
    await service.poll_once()

    clock.now = first_end + datetime.timedelta(hours=1)
    subscription["current_period_end"] = (first_end + datetime.timedelta(days=30)).isoformat()  # Polar renewed it
    await service.poll_once()

    assert (await service.access(_GUILD)).until == first_end + datetime.timedelta(days=30)
    assert announced == ["sub_1"]


async def test_a_refund_or_chargeback_that_ends_the_subscription_early_cuts_access(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    subscription = polar.subscribe(status="active", period_end=period_end)
    await service.poll_once()

    clock.advance(days=2)
    subscription.update(status="canceled", ended_at=clock.now.isoformat())  # revoked at once; period_end unchanged
    clock.advance(minutes=11)
    await service.poll_once()

    assert _row(db)["paid_until"] == subscription["ended_at"]
    clock.advance(days=4)  # past the grace period — long before the old period end
    assert (await service.allowed_guild_ids({_GUILD})) == set()


async def test_ending_the_card_subscription_early_doesnt_cut_prepaid_crypto_time(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    nowpayments = FakeNowPayments()
    service = _service(db, clock, polar=polar, nowpayments=nowpayments)
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    subscription = polar.subscribe(status="active", period_end=period_end)
    await service.poll_once()
    nowpayments.create("1", price_usd=8.97, order_id=f"{_GUILD}-a", status="finished")
    _website_row(db, "nowpayments", "1", months=3, amount_usd=8.97, created_at=clock.now)
    await service.poll_once()  # +90 days on top of the card month

    subscription.update(status="canceled", ended_at=(clock.now + datetime.timedelta(days=1)).isoformat())
    clock.advance(minutes=11)
    await service.poll_once()

    assert (await service.access(_GUILD)).until == period_end + datetime.timedelta(days=90)


async def test_subscribing_again_after_cancelling_replaces_the_old_subscription(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    clock.advance(days=8)
    old = polar.subscribe("sub_old", status="active", period_end=clock.now + datetime.timedelta(days=5))
    await service.poll_once()
    old.update(status="canceled", ended_at=(clock.now + datetime.timedelta(days=5)).isoformat())
    clock.advance(days=6)
    polar.subscribe("sub_new", status="active", period_end=clock.now + datetime.timedelta(days=30))

    await service.poll_once()

    assert _row(db)["card_subscription_id"] == "sub_new" and _row(db)["card_status"] == "active"
    assert sorted(p["external_id"] for p in db.tables["notify_payments"].rows) == ["sub_new", "sub_old"]


async def test_a_polar_outage_doesnt_stop_crypto_payments_being_credited(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    polar.list_fails = True
    nowpayments = FakeNowPayments()
    service = _service(db, clock, polar=polar, nowpayments=nowpayments)
    await service.access(_GUILD)
    nowpayments.create("1", price_usd=8.97, order_id=f"{_GUILD}-a", status="finished")
    _website_row(db, "nowpayments", "1", months=3, amount_usd=8.97, created_at=clock.now)

    await service.poll_once()

    assert (await service.access(_GUILD)).reason == AccessReason.PAID


async def test_the_checkout_carries_the_server_the_requester_and_the_remaining_trial(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar, polar_success_url="https://disconotify.com/thanks")
    await _activated(service, db, clock)
    clock.advance(days=2, hours=1)  # 4 days 23 hours of trial left

    url = await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    clock.advance(days=9)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)

    assert url == "https://buy.polar.sh/co_1"
    assert polar.checkouts[0] == {
        "product_id": "prod_1",
        "metadata": {"guild_id": "1", "user_id": "7", "channel_id": "555"},
        "trial_days": 5,  # rounded up: the card isn't charged before the trial ends
        "success_url": "https://disconotify.com/thanks",
    }
    assert polar.checkouts[1]["trial_days"] == 0  # trial over — charged straight away


async def test_a_checkout_link_is_reused_for_ten_minutes(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)

    first = await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    clock.advance(minutes=9)
    again = await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    other_admin = await service.start_card_checkout(_GUILD, user_id=8, channel_id=555)
    clock.advance(minutes=2)
    later = await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)

    assert first == again and len({first, other_admin, later}) == 3
    assert len(polar.checkouts) == 3


@pytest.mark.parametrize("bad_url", ["https://evil.example/pay", "http://buy.polar.sh/x", "javascript:alert(1)", "https://polar.sh.evil.example/x", ""])
async def test_links_that_arent_https_on_polars_own_domain_are_refused(db: FakeDatabase, clock: Clock, bad_url: Any) -> None:
    polar = FakePolar()
    polar.checkout_url = bad_url
    polar.portal_url = bad_url
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    clock.advance(days=8)
    polar.subscribe(status="active", period_end=clock.now + datetime.timedelta(days=30))
    await service.poll_once()

    with pytest.raises(BillingUnavailable):
        await service.start_card_checkout(_GUILD)
    with pytest.raises(BillingUnavailable):
        await service.manage_card_url(_GUILD)


async def test_the_billing_portal_is_for_the_payer_or_the_server_owner_only(db: FakeDatabase, clock: Clock) -> None:
    """The portal link is pre-authenticated as the paying customer — it shows
    their email and card — so other admins don't get it."""
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    await service.access(_GUILD)
    clock.advance(days=8)
    polar.subscribe(status="active", period_end=clock.now + datetime.timedelta(days=30), user_id=7)
    await service.poll_once()

    assert await service.can_manage_card(_GUILD, 7, server_owner_id=99) is True  # the payer
    assert await service.can_manage_card(_GUILD, 99, server_owner_id=99) is True  # the server owner
    assert await service.can_manage_card(_GUILD, 8, server_owner_id=99) is False  # another admin
    assert await service.card_payer_id(_GUILD) == 7
    assert (await service.manage_card_url(_GUILD)).startswith("https://polar.sh/")
    assert polar.sessions == ["cus_1"]


async def test_a_server_with_no_card_subscription_has_nothing_to_manage(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)

    with pytest.raises(BillingUnavailable):
        await service.manage_card_url(_GUILD)
    assert await service.can_manage_card(_GUILD, 7, server_owner_id=99) is False


async def test_card_is_unavailable_without_polar(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock, polar=None)

    await service.poll_once()

    assert service.card_available is False
    with pytest.raises(BillingUnavailable):
        await service.start_card_checkout(_GUILD)


async def test_an_old_pending_non_crypto_row_expires(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    _website_row(db, "paddle", "txn_9", months=1, amount_usd=2.99)  # left over from the Paddle days
    clock.advance(days=2)

    await service.poll_once()

    assert db.tables["notify_payments"].rows[0]["status"] == "expired"


async def test_the_startup_check_warns_when_the_polar_price_differs(db: FakeDatabase, clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    polar = FakePolar()
    polar.product = {"prices": [{"price_amount": 100}], "is_recurring": True}
    service = _service(db, clock, polar=polar)

    with caplog.at_level("WARNING"):
        await service.check_setup()

    assert "must match" in caplog.text


# ── crypto (NOWPayments, payment created on the website) ────────────────


async def test_crypto_prepayment_adds_months_once_confirmed(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    clock.advance(days=8)
    nowpayments.create("5000", price_usd=8.97, order_id=f"{_GUILD}-abc")
    _website_row(db, "nowpayments", "5000", months=3, amount_usd=8.97, created_at=clock.now)

    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()  # still waiting

    nowpayments.payments["5000"]["payment_status"] = "confirmed"
    await service.poll_once()
    await service.poll_once()  # already credited — must not add another 3 months

    access = await service.access(_GUILD)
    assert access.reason == AccessReason.PAID
    assert access.until == clock.now + datetime.timedelta(days=90)


async def test_a_second_crypto_payment_stacks_on_the_first(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    nowpayments.create("1", price_usd=8.97, order_id=f"{_GUILD}-a", status="finished")
    nowpayments.create("2", price_usd=17.94, order_id=f"{_GUILD}-b", status="finished")
    _website_row(db, "nowpayments", "1", months=3, amount_usd=8.97)
    _website_row(db, "nowpayments", "2", months=6, amount_usd=17.94)

    await service.poll_once()

    assert (await service.access(_GUILD)).until == clock.now + datetime.timedelta(days=270)


async def test_failed_and_expired_crypto_payments_grant_nothing(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    nowpayments.create("1", price_usd=8.97, order_id=f"{_GUILD}-a", status="failed")
    nowpayments.create("2", price_usd=8.97, order_id=f"{_GUILD}-b", status="expired")
    _website_row(db, "nowpayments", "1", months=3, amount_usd=8.97)
    _website_row(db, "nowpayments", "2", months=3, amount_usd=8.97)

    await service.poll_once()

    statuses = sorted(row["status"] for row in db.tables["notify_payments"].rows)
    assert statuses == ["expired", "failed"]
    assert _row(db).get("paid_until") is None


@pytest.mark.parametrize(
    ("price_usd", "order_id"),
    [(2.99, f"{_GUILD}-a"), (35.88, "777-a")],  # underpaid for 12 months · paid for another server
)
async def test_a_crypto_row_that_doesnt_match_nowpayments_is_not_credited(
    db: FakeDatabase, clock: Clock, price_usd: float, order_id: str
) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    nowpayments.create("1", price_usd=price_usd, order_id=order_id, status="finished")
    _website_row(db, "nowpayments", "1", months=12, amount_usd=35.88)

    await service.poll_once()

    assert db.tables["notify_payments"].rows[0]["status"] == "failed"
    assert _row(db).get("paid_until") is None


# ── website links (crypto page) ─────────────────────────────────────────


def test_website_links_carry_the_server_id(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock, subscribe_url="https://disconotify.com/subscribe?ref=bot")

    assert service.subscribe_url(42) == "https://disconotify.com/subscribe?ref=bot&guild_id=42"
    assert with_server("https://x.org/servers/{guild_id}/pay", 42) == "https://x.org/servers/42/pay"
    assert _service(db, clock).subscribe_url(42) is None


# ── upgrade reminders ───────────────────────────────────────────────────


async def test_reminders_start_when_the_trial_ends_then_repeat_weekly_up_to_three(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await _activated(service, db, clock)

    async def remind() -> bool:
        due = await service.reminders_due({_GUILD})
        for row in due:
            await service.mark_reminded(row)
        return bool(due)

    assert await remind() is False  # still in the trial
    clock.advance(days=7, minutes=1)
    assert await remind() is True  # trial just ended
    clock.advance(days=6)
    assert await remind() is False
    clock.advance(days=1)
    assert await remind() is True  # a week later
    clock.advance(days=7)
    assert await remind() is True  # third and last
    clock.advance(days=30)
    assert await remind() is False
    assert _row(db)["reminders_sent"] == 3


async def test_paying_resets_the_reminders_for_the_next_lapse(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await _activated(service, db, clock)
    clock.advance(days=8)
    for row in await service.reminders_due({_GUILD}):
        await service.mark_reminded(row)

    _row(db)["paid_until"] = (clock.now + datetime.timedelta(days=30)).isoformat()
    assert await service.reminders_due({_GUILD}) == []
    assert _row(db)["reminders_sent"] == 0

    # Once the paid time (and grace) ran out, the lapsed subscription is reminded about from the start.
    clock.advance(days=34)
    assert [row["guild_id"] for row in await service.reminders_due({_GUILD})] == [_GUILD]


async def test_a_server_that_never_activated_gets_no_trial_reminders(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    clock.advance(days=30)

    assert await service.reminders_due({_GUILD}) == []


async def test_the_reminder_schedule_can_be_overridden(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await _activated(service, db, clock)
    clock.advance(days=8)
    for row in await service.reminders_due({_GUILD}, every_days=2, maximum=2):
        await service.mark_reminded(row)

    clock.advance(days=1)
    assert await service.reminders_due({_GUILD}, every_days=2, maximum=2) == []
    clock.advance(days=1)
    for row in await service.reminders_due({_GUILD}, every_days=2, maximum=2):
        await service.mark_reminded(row)
    clock.advance(days=10)
    assert await service.reminders_due({_GUILD}, every_days=2, maximum=2) == []


async def test_whitelisted_servers_and_billing_off_get_no_reminders(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.whitelist(_GUILD, by=1, note=None)
    clock.advance(days=30)

    assert await service.reminders_due({_GUILD}) == []
    assert await _service(db, clock, enabled=False).reminders_due({_GUILD}) == []


# ── provider clients ────────────────────────────────────────────────────


def _polar_client(handler: Any, **kwargs: Any) -> PolarClient:
    return PolarClient("polar_oat_secret", transport=httpx.MockTransport(handler), **kwargs)


async def test_polar_checkout_request_shape() -> None:
    sent: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"id": "co_1", "url": "https://buy.polar.sh/co_1"})

    client = _polar_client(handler)
    await client.create_checkout("prod_1", metadata={"guild_id": "1"}, trial_days=5, success_url="https://x.org/thanks")
    await client.create_checkout("prod_1", metadata={"guild_id": "1"})

    with_trial, without = (json.loads(request.content) for request in sent)
    assert sent[0].url.path == "/v1/checkouts/" and sent[0].method == "POST"
    assert sent[0].headers["Authorization"] == "Bearer polar_oat_secret"
    assert with_trial == {
        "products": ["prod_1"],
        "metadata": {"guild_id": "1"},
        "allow_trial": True,
        "trial_interval": "day",
        "trial_interval_count": 5,
        "success_url": "https://x.org/thanks",
    }
    assert without == {"products": ["prod_1"], "metadata": {"guild_id": "1"}, "allow_trial": False}


async def test_polar_lists_every_page_of_subscriptions() -> None:
    seen: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen.append({"page": page, "status": request.url.params.get_list("status"), **{k: request.url.params.get(k) for k in ("limit", "sorting", "started_after")}})
        return httpx.Response(200, json={"items": [{"id": f"sub_{page}"}], "pagination": {"total_count": 2, "max_page": 2}})

    client = _polar_client(handler)
    items = await client.list_subscriptions(started_after=_START, statuses=["active", "trialing"])

    assert [item["id"] for item in items] == ["sub_1", "sub_2"]
    assert seen[0] == {
        "page": 1, "status": ["active", "trialing"], "limit": "100", "sorting": "-started_at", "started_after": _START.isoformat()
    }


async def test_polar_customer_session_request_shape() -> None:
    sent: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"customer_portal_url": "https://polar.sh/x/portal?customer_session_token=t"})

    session = await _polar_client(handler).create_customer_session("cus_1")

    assert sent[0].url.path == "/v1/customer-sessions/" and json.loads(sent[0].content) == {"customer_id": "cus_1"}
    assert session["customer_portal_url"].startswith("https://polar.sh/")


async def test_polar_sandbox_uses_the_sandbox_host() -> None:
    hosts: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={})

    await _polar_client(handler, sandbox=True).get_product("prod_1")

    assert hosts == ["sandbox-api.polar.sh"]


@pytest.mark.parametrize(
    ("status", "error", "text"),
    [
        (401, PolarError, "checkouts:write"),
        (403, PolarError, "rejected the access token"),
        (404, PermanentPlatformError, "Polar 404"),
        (422, PermanentPlatformError, "products: Field required"),
        (500, TransientPlatformError, "Polar error 500"),
    ],
)
async def test_polar_errors_are_classified(status: int, error: type, text: str) -> None:
    body = {"detail": [{"loc": ["body", "products"], "msg": "Field required"}]} if status == 422 else {"detail": "nope"}
    client = _polar_client(lambda request: httpx.Response(status, json=body))

    with pytest.raises(error, match=text):
        await client.get_subscription("sub_1")


async def test_polar_rate_limits_report_the_wait() -> None:
    client = _polar_client(lambda request: httpx.Response(429, json={"detail": "slow down"}, headers={"retry-after": "30"}))

    with pytest.raises(RateLimitedError) as exc_info:
        await client.get_subscription("sub_1")

    assert exc_info.value.retry_after == 30


async def test_a_polar_timeout_is_temporary_and_never_leaks_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientPlatformError) as exc_info:
        await _polar_client(handler).get_subscription("sub_1")

    assert "polar_oat_secret" not in str(exc_info.value)


async def test_nowpayments_sends_the_api_key_and_usd_price() -> None:
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"payment_id": 1, "pay_address": "T...", "pay_amount": 3.01})

    client = NowPaymentsClient("np-key", transport=httpx.MockTransport(handler))
    await client.create_payment(price_usd=3.0, pay_currency="usdttrc20", order_id="1-abc", description="test")

    assert seen["key"] == "np-key"
    assert seen["body"]["price_amount"] == 3.0 and seen["body"]["price_currency"] == "usd"
    assert seen["body"]["pay_currency"] == "usdttrc20"


# ── price display ───────────────────────────────────────────────────────


def test_prices_display_without_needless_cents() -> None:
    assert format_usd(1) == "$1"
    assert format_usd(3.0) == "$3"
    assert format_usd(2.5) == "$2.50"


async def test_nowpayments_min_amount_request_shape() -> None:
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"currency_from": "usdttrc20", "min_amount": 11.16, "fiat_equivalent": 11.18})

    client = NowPaymentsClient("np-key", transport=httpx.MockTransport(handler))

    assert await client.min_amount_usd("usdttrc20") == 11.18
    assert seen == {"currency_from": "usdttrc20", "currency_to": "usdttrc20", "fiat_equivalent": "usd"}


# ── alerts for what happens after a subscription begins ─────────────────


async def test_renewals_failed_charges_cancellations_and_endings_each_raise_one_alert(db: FakeDatabase, clock: Clock) -> None:
    polar = FakePolar()
    service = _service(db, clock, polar=polar)
    events: List[str] = []

    async def on_event(event: Any) -> None:
        events.append(event.kind)

    service.on_event = on_event
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    subscription = polar.subscribe(status="active", period_end=period_end)
    await service.poll_once()
    assert events == []  # the first payment is the "payment received" card, not an event

    clock.advance(days=30, minutes=11)
    subscription.update(current_period_end=(period_end + datetime.timedelta(days=30)).isoformat())
    await service.poll_once()
    clock.advance(minutes=11)
    await service.poll_once()  # nothing new changed — no repeat
    subscription.update(status="past_due")
    clock.advance(minutes=11)
    await service.poll_once()
    subscription.update(status="active", cancel_at_period_end=True, ends_at=subscription["current_period_end"])
    clock.advance(minutes=11)
    await service.poll_once()
    subscription.update(status="canceled", ended_at=subscription["current_period_end"])
    clock.advance(minutes=11)
    await service.poll_once()

    assert events == ["card_renewed", "card_failed", "card_cancel_scheduled", "card_ended"]


async def test_failed_and_expired_crypto_payments_raise_alerts(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    events: List[str] = []

    async def on_event(event: Any) -> None:
        events.append(event.kind)

    service.on_event = on_event
    await service.access(_GUILD)
    nowpayments.create("1", price_usd=8.97, order_id=f"{_GUILD}-a", status="failed")
    nowpayments.create("2", price_usd=8.97, order_id=f"{_GUILD}-b", status="expired")
    _website_row(db, "nowpayments", "1", months=3, amount_usd=8.97)
    _website_row(db, "nowpayments", "2", months=3, amount_usd=8.97)

    await service.poll_once()
    await service.poll_once()

    assert sorted(events) == ["crypto_expired", "crypto_failed"]


async def test_a_crypto_payment_nowpayments_has_never_heard_of_is_closed_quietly_after_the_window(
    db: FakeDatabase, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Made under another API key or in the other sandbox/live mode: not an
    error to dump on every poll, and no alert to the server when it closes."""
    from billing.nowpayments import PaymentNotFound

    class Unknown(FakeNowPayments):
        async def get_payment(self, payment_id: str):
            raise PaymentNotFound("NOWPayments: Payment not found")

    events = []
    service = _service(db, clock, nowpayments=Unknown())

    async def on_event(event: Any) -> None:
        events.append(event)

    service.on_event = on_event
    _website_row(db, "nowpayments", "np-1", months=1, amount_usd=5.0, created_at=clock.now)

    with caplog.at_level("WARNING"):
        await service.poll_once()
        await service.poll_once()
    assert len([r for r in caplog.records if "NOWPayments has no payment" in r.message]) == 1  # once, no traceback
    assert not any(r.exc_info for r in caplog.records)
    assert db.tables["notify_payments"].rows[0]["status"] == "pending"

    clock.advance(days=4)
    await service.poll_once()

    assert db.tables["notify_payments"].rows[0]["status"] == "expired"
    assert events == []
