"""Billing: access rules, the trial, card subscriptions (Paddle) including
cancellation, crypto prepayments (NOWPayments), the whitelist, and the
payment poller — driven through fake provider clients and the fake
database, with an injectable clock."""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from billing.access import Access, AccessReason, describe_access, evaluate
from billing.nowpayments import NowPaymentsClient
from billing.paddle import PaddleClient
from billing.repository import BillingRepository
from billing.service import BillingService, BillingUnavailable, format_usd
from tests.unit.fakes import FakeDatabase

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


class FakePaddle:
    def __init__(self) -> None:
        self.transactions: Dict[str, Dict[str, Any]] = {}
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.created: List[Dict[str, Any]] = []

    async def create_transaction(
        self, price_id: str, custom_data: Dict[str, str], *, trial_days: int = 0, amount_cents: Optional[int] = None
    ):
        txn_id = f"txn_{len(self.created) + 1}"
        self.created.append(
            {"price_id": price_id, "custom_data": custom_data, "trial_days": trial_days, "amount_cents": amount_cents}
        )
        self.transactions[txn_id] = {"id": txn_id, "status": "ready", "subscription_id": None}
        return {"id": txn_id, "checkout": {"url": f"https://pay.example/?_ptxn={txn_id}"}}

    async def get_transaction(self, transaction_id: str):
        return self.transactions[transaction_id]

    async def get_subscription(self, subscription_id: str):
        return self.subscriptions[subscription_id]

    async def create_portal_session(self, customer_id: str, subscription_ids: List[str]):
        return {"urls": {"general": {"overview": f"https://portal.example/{customer_id}"}}}

    async def aclose(self) -> None:
        pass

    def pay(self, txn_id: str, *, status: str, period_end: datetime.datetime, cancel_at: Optional[datetime.datetime] = None):
        """Simulates the customer completing checkout."""
        self.transactions[txn_id].update(status="completed", subscription_id="sub_1")
        self.set_subscription(status=status, period_end=period_end, cancel_at=cancel_at)

    def set_subscription(self, *, status: str, period_end: datetime.datetime, cancel_at: Optional[datetime.datetime] = None):
        self.subscriptions["sub_1"] = {
            "id": "sub_1",
            "customer_id": "ctm_1",
            "status": status,
            "current_billing_period": {"ends_at": period_end.isoformat()},
            "scheduled_change": {"action": "cancel", "effective_at": cancel_at.isoformat()} if cancel_at else None,
        }


class FakeNowPayments:
    def __init__(self) -> None:
        self.payments: Dict[str, Dict[str, Any]] = {}

    async def create_payment(self, *, price_usd: float, pay_currency: str, order_id: str, description: str):
        payment_id = str(5000 + len(self.payments))
        self.payments[payment_id] = {"payment_id": payment_id, "payment_status": "waiting"}
        return {
            "payment_id": payment_id,
            "pay_address": "TXyzAddress",
            "pay_amount": price_usd,
            "pay_currency": pay_currency,
        }

    async def get_payment(self, payment_id: str):
        return self.payments[payment_id]

    async def aclose(self) -> None:
        pass


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    database.tables["pulsenotify_guilds"].rows.append({"guild_id": _GUILD, "enabled": True})
    return database


def _service(db: FakeDatabase, clock: Clock, **kwargs: Any) -> BillingService:
    defaults: Dict[str, Any] = {"enabled": True, "paddle": FakePaddle(), "paddle_price_id": "pri_1", "nowpayments": FakeNowPayments()}
    defaults.update(kwargs)
    return BillingService(BillingRepository(db), clock=clock, **defaults)  # type: ignore[arg-type]


def _row(db: FakeDatabase) -> Dict[str, Any]:
    return db.tables["pulsenotify_billing"].rows[0]


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
    assert db.tables["pulsenotify_billing"].rows == []


async def test_a_server_gets_a_7_day_trial_then_loses_access(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)

    first = await service.access(_GUILD)
    clock.advance(days=8)
    later = await service.allowed_guild_ids({_GUILD})

    assert first.reason == AccessReason.TRIAL and first.until == _START + datetime.timedelta(days=7)
    assert later == set()


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


# ── card (Paddle) ───────────────────────────────────────────────────────


async def test_card_checkout_during_the_trial_defers_the_first_charge_to_the_trial_end(db: FakeDatabase, clock: Clock) -> None:
    """Subscribing during the trial must not charge until it ends."""
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    clock.advance(days=2, hours=1)  # 4 days 23 hours of trial left

    url = await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)

    assert url.startswith("https://pay.example/")
    assert paddle.created[0]["trial_days"] == 5  # rounded up
    assert db.tables["pulsenotify_payments"].rows[0]["status"] == "pending"


async def test_card_checkout_after_the_trial_charges_straight_away(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    clock.advance(days=9)

    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)

    assert paddle.created[0]["trial_days"] == 0


async def test_a_paid_card_subscription_grants_access_until_the_period_ends(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    announced: List[Any] = []

    async def on_payment(payment: Dict[str, Any], access: Access) -> None:
        announced.append((payment["provider"], access.reason))

    service.on_payment = on_payment
    await service.access(_GUILD)
    clock.advance(days=8)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    period_end = clock.now + datetime.timedelta(days=30)
    paddle.pay("txn_1", status="active", period_end=period_end)

    await service.poll_once()

    access = await service.access(_GUILD)
    assert access.reason == AccessReason.PAID and access.until == period_end
    assert db.tables["pulsenotify_payments"].rows[0]["status"] == "paid"
    assert announced == [("paddle", AccessReason.PAID)]
    assert "renews automatically by card" in describe_access(access, _row(db))


async def test_cancelling_during_the_trial_is_never_charged(db: FakeDatabase, clock: Clock) -> None:
    """Card saved during the trial, then cancelled before it ends: Paddle
    cancels at the trial end and nothing was ever charged — access simply
    ends with the trial."""
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    trial_end = _START + datetime.timedelta(days=7)
    paddle.pay("txn_1", status="trialing", period_end=trial_end)
    await service.poll_once()

    during = await service.access(_GUILD)
    assert during.reason == AccessReason.TRIAL
    assert _row(db).get("paid_until") is None  # trialing isn't paid time
    assert "card subscription starts" in describe_access(during, _row(db))

    paddle.set_subscription(status="trialing", period_end=trial_end, cancel_at=trial_end)  # customer cancels
    clock.advance(minutes=11)
    await service.poll_once()
    assert "Free trial — ends" in describe_access(await service.access(_GUILD), _row(db))

    clock.now = trial_end + datetime.timedelta(minutes=1)
    paddle.set_subscription(status="canceled", period_end=trial_end)
    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()
    assert _row(db)["card_status"] == "canceled"


async def test_cancelling_after_paying_keeps_access_until_the_paid_month_ends(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    clock.advance(days=8)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    period_end = clock.now + datetime.timedelta(days=30)
    paddle.pay("txn_1", status="active", period_end=period_end)
    await service.poll_once()

    paddle.set_subscription(status="active", period_end=period_end, cancel_at=period_end)  # cancels, no renewal
    clock.advance(minutes=11)
    await service.poll_once()
    still_paid = await service.access(_GUILD)

    assert still_paid.reason == AccessReason.PAID and still_paid.until == period_end
    assert "stops unless renewed" in describe_access(still_paid, _row(db))

    clock.now = period_end + datetime.timedelta(days=4)  # past the grace period
    paddle.set_subscription(status="canceled", period_end=period_end)
    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()


async def test_monthly_renewal_extends_access(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    clock.advance(days=8)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    first_end = clock.now + datetime.timedelta(days=30)
    paddle.pay("txn_1", status="active", period_end=first_end)
    await service.poll_once()

    clock.now = first_end + datetime.timedelta(hours=1)
    paddle.set_subscription(status="active", period_end=first_end + datetime.timedelta(days=30))
    await service.poll_once()

    assert (await service.access(_GUILD)).until == first_end + datetime.timedelta(days=30)


async def test_an_abandoned_card_checkout_expires(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    db.tables["pulsenotify_payments"].rows[0]["created_at"] = (clock.now - datetime.timedelta(days=2)).isoformat()

    await service.poll_once()

    assert db.tables["pulsenotify_payments"].rows[0]["status"] == "expired"


async def test_card_not_configured_is_reported(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock, paddle=None)

    with pytest.raises(BillingUnavailable):
        await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    assert service.card_available is False


async def test_manage_link_comes_from_paddles_portal(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    await service.start_card_checkout(_GUILD, user_id=7, channel_id=555)
    paddle.pay("txn_1", status="trialing", period_end=_START + datetime.timedelta(days=7))
    await service.poll_once()

    assert await service.manage_card_url(_GUILD) == "https://portal.example/ctm_1"


# ── crypto (NOWPayments) ────────────────────────────────────────────────


async def test_crypto_prepayment_adds_months_once_confirmed(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    clock.advance(days=8)

    payment = await service.start_crypto_payment(_GUILD, months=3, coin="usdttrc20", user_id=7, channel_id=555)
    assert payment["pay_amount"] == 8.97  # 3 × the $2.99 default
    await service.poll_once()
    assert (await service.allowed_guild_ids({_GUILD})) == set()  # still waiting

    nowpayments.payments[payment["payment_id"]]["payment_status"] = "confirmed"
    await service.poll_once()
    await service.poll_once()  # already credited — must not add another 3 months

    access = await service.access(_GUILD)
    assert access.reason == AccessReason.PAID
    assert access.until == clock.now + datetime.timedelta(days=90)


async def test_a_second_crypto_payment_stacks_on_the_first(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    first = await service.start_crypto_payment(_GUILD, months=3, coin="ltc", user_id=7, channel_id=None)
    second = await service.start_crypto_payment(_GUILD, months=6, coin="ltc", user_id=7, channel_id=None)
    for payment in (first, second):
        nowpayments.payments[payment["payment_id"]]["payment_status"] = "finished"

    await service.poll_once()

    assert (await service.access(_GUILD)).until == clock.now + datetime.timedelta(days=270)


async def test_failed_and_expired_crypto_payments_grant_nothing(db: FakeDatabase, clock: Clock) -> None:
    nowpayments = FakeNowPayments()
    service = _service(db, clock, nowpayments=nowpayments)
    await service.access(_GUILD)
    failed = await service.start_crypto_payment(_GUILD, months=3, coin="btc", user_id=7, channel_id=None)
    expired = await service.start_crypto_payment(_GUILD, months=3, coin="btc", user_id=7, channel_id=None)
    nowpayments.payments[failed["payment_id"]]["payment_status"] = "failed"
    nowpayments.payments[expired["payment_id"]]["payment_status"] = "expired"

    await service.poll_once()

    statuses = sorted(row["status"] for row in db.tables["pulsenotify_payments"].rows)
    assert statuses == ["expired", "failed"]
    assert _row(db).get("paid_until") is None


async def test_unsupported_plan_is_rejected(db: FakeDatabase, clock: Clock) -> None:
    with pytest.raises(ValueError):
        await _service(db, clock).start_crypto_payment(_GUILD, months=1, coin="btc", user_id=7, channel_id=None)


# ── provider clients ────────────────────────────────────────────────────


async def test_paddle_trial_checkout_copies_the_price_with_a_trial_period() -> None:
    sent: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer key"
        if request.method == "GET":
            price = {
                "id": "pri_1",
                "product_id": "pro_1",
                "unit_price": {"amount": "100", "currency_code": "USD"},
                "billing_cycle": {"interval": "month", "frequency": 1},
            }
            return httpx.Response(200, json={"data": price})
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"data": {"id": "txn_1", "checkout": {"url": "https://pay.example"}}})

    client = PaddleClient("key", sandbox=True, transport=httpx.MockTransport(handler))
    await client.create_transaction("pri_1", {"guild_id": "1"}, trial_days=5)
    await client.create_transaction("pri_1", {"guild_id": "1"})

    trial_item, plain_item = sent[0]["items"][0], sent[1]["items"][0]
    assert trial_item["price"]["product_id"] == "pro_1"
    assert trial_item["price"]["trial_period"] == {"interval": "day", "frequency": 5}
    assert trial_item["price"]["unit_price"] == {"amount": "100", "currency_code": "USD"}
    assert plain_item == {"price_id": "pri_1", "quantity": 1}
    assert sent[0]["custom_data"] == {"guild_id": "1"}


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


# ── price setting (BILLING_PRICE_USD) ───────────────────────────────────


def test_prices_display_without_needless_cents() -> None:
    assert format_usd(1) == "$1"
    assert format_usd(3.0) == "$3"
    assert format_usd(2.5) == "$2.50"


async def test_the_price_setting_drives_card_and_crypto(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    nowpayments = FakeNowPayments()
    service = _service(db, clock, paddle=paddle, nowpayments=nowpayments, price_usd=2.5)
    await service.access(_GUILD)

    await service.start_card_checkout(_GUILD, user_id=7, channel_id=None)
    crypto = await service.start_crypto_payment(_GUILD, months=6, coin="ltc", user_id=7, channel_id=None)

    assert service.price_label == "$2.50"
    assert paddle.created[0]["amount_cents"] == 250
    assert crypto["pay_amount"] == 15.0  # 6 × $2.50
    amounts = sorted(float(row["amount_usd"]) for row in db.tables["pulsenotify_payments"].rows)
    assert amounts == [2.5, 15.0]


async def test_paddle_checkout_charges_the_configured_amount_not_the_catalog_one() -> None:
    sent: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            price = {
                "product_id": "pro_1",
                "unit_price": {"amount": "100", "currency_code": "USD"},
                "billing_cycle": {"interval": "month", "frequency": 1},
            }
            return httpx.Response(200, json={"data": price})
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"data": {"id": "txn_1", "checkout": {"url": "https://pay.example"}}})

    client = PaddleClient("key", transport=httpx.MockTransport(handler))
    await client.create_transaction("pri_1", {"guild_id": "1"}, amount_cents=300)

    price = sent[0]["items"][0]["price"]
    assert price["unit_price"] == {"amount": "300", "currency_code": "USD"}
    assert price["billing_cycle"] == {"interval": "month", "frequency": 1}
    assert "trial_period" not in price
