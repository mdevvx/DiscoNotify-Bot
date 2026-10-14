"""Billing: access rules, the trial, website card subscriptions (Paddle)
including cancellation, website crypto prepayments (NOWPayments), the
whitelist, upgrade reminders and the payment poller — driven through fake
provider clients and the fake database, with an injectable clock."""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from billing.access import Access, AccessReason, describe_access, evaluate
from billing.nowpayments import NowPaymentsClient
from billing.paddle import PaddleClient, is_sandbox_key
from billing.repository import BillingRepository
from billing.service import BillingService, format_usd, with_server
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
    """Paddle as the bot sees it: transactions the website's checkout
    created (with custom_data.guild_id) and the subscriptions behind them."""

    def __init__(self) -> None:
        self.transactions: Dict[str, Dict[str, Any]] = {}
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.listed_after: List[str] = []

    async def list_paid_transactions(self, *, updated_after: str, per_page: int = 100):
        self.listed_after.append(updated_after)
        after = datetime.datetime.fromisoformat(updated_after)
        paid = [t for t in self.transactions.values() if t["status"] in ("paid", "completed")]
        newer = [t for t in paid if datetime.datetime.fromisoformat(t["updated_at"]) > after]
        return sorted(newer, key=lambda t: t["updated_at"])

    async def get_transaction(self, transaction_id: str):
        return self.transactions[transaction_id]

    async def get_subscription(self, subscription_id: str):
        return self.subscriptions[subscription_id]

    async def aclose(self) -> None:
        pass

    def checkout(
        self,
        txn_id: str,
        *,
        status: str,
        period_end: datetime.datetime,
        at: datetime.datetime,
        guild_id: Any = _GUILD,
        cancel_at: Optional[datetime.datetime] = None,
    ) -> None:
        """Simulates a customer completing checkout on the website."""
        self.transactions[txn_id] = {
            "id": txn_id,
            "status": "completed",
            "subscription_id": "sub_1",
            "custom_data": {"guild_id": str(guild_id)},
            "updated_at": at.isoformat(),
        }
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
    database.tables["pulsenotify_guilds"].rows.append({"guild_id": _GUILD, "enabled": True})
    return database


def _service(db: FakeDatabase, clock: Clock, **kwargs: Any) -> BillingService:
    defaults: Dict[str, Any] = {"enabled": True, "paddle": FakePaddle(), "nowpayments": FakeNowPayments()}
    defaults.update(kwargs)
    return BillingService(BillingRepository(db), clock=clock, **defaults)  # type: ignore[arg-type]


def _row(db: FakeDatabase) -> Dict[str, Any]:
    return db.tables["pulsenotify_billing"].rows[0]


def _website_row(
    db: FakeDatabase, provider: str, external_id: str, *, months: int, amount_usd: float, created_at: datetime.datetime = _START
) -> None:
    """What the website inserts after creating a payment."""
    db.tables["pulsenotify_payments"].rows.append(
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


# ── card (Paddle, checkout on the website) ──────────────────────────────


async def test_a_website_card_checkout_is_found_and_grants_access_until_the_period_ends(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    announced: List[Any] = []

    async def on_payment(payment: Dict[str, Any], access: Access) -> None:
        announced.append((payment["provider"], payment["requested_by"], payment["channel_id"], access.reason))

    service.on_payment = on_payment
    await service.access(_GUILD)
    clock.advance(days=8)
    period_end = clock.now + datetime.timedelta(days=30)
    paddle.checkout("txn_1", status="active", period_end=period_end, at=clock.now)

    await service.poll_once()
    await service.poll_once()  # already credited — no second confirmation

    access = await service.access(_GUILD)
    assert access.reason == AccessReason.PAID and access.until == period_end
    payments = db.tables["pulsenotify_payments"].rows
    assert [(p["external_id"], p["status"]) for p in payments] == [("txn_1", "paid")]
    assert announced == [("paddle", None, None, AccessReason.PAID)]
    assert "renews automatically by card" in describe_access(access, _row(db))


async def test_checkouts_without_a_server_id_or_for_unknown_servers_are_ignored(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    end = clock.now + datetime.timedelta(days=30)
    paddle.checkout("txn_1", status="active", period_end=end, at=clock.now, guild_id="")
    paddle.checkout("txn_2", status="active", period_end=end, at=clock.now, guild_id=999)

    await service.poll_once()

    assert db.tables["pulsenotify_payments"].rows == []
    assert db.tables["pulsenotify_billing"].rows == []


async def test_discovery_only_asks_for_transactions_newer_than_the_last_seen(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    seen_at = clock.now + datetime.timedelta(minutes=1)
    paddle.checkout("txn_1", status="active", period_end=clock.now + datetime.timedelta(days=30), at=seen_at)

    await service.poll_once()
    await service.poll_once()

    assert paddle.listed_after[0] == (_START - datetime.timedelta(days=3)).isoformat()
    assert paddle.listed_after[1] == seen_at.isoformat()


async def test_cancelling_during_the_trial_is_never_charged(db: FakeDatabase, clock: Clock) -> None:
    """Card saved during the trial, then cancelled before it ends: Paddle
    cancels at the trial end and nothing was ever charged — access simply
    ends with the trial."""
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    trial_end = _START + datetime.timedelta(days=7)
    paddle.checkout("txn_1", status="trialing", period_end=trial_end, at=clock.now)
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
    period_end = clock.now + datetime.timedelta(days=30)
    paddle.checkout("txn_1", status="active", period_end=period_end, at=clock.now)
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


async def test_monthly_renewal_extends_access_without_a_new_confirmation(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    announced: List[str] = []

    async def on_payment(payment: Dict[str, Any], access: Access) -> None:
        announced.append(payment["external_id"])

    service.on_payment = on_payment
    await service.access(_GUILD)
    clock.advance(days=8)
    first_end = clock.now + datetime.timedelta(days=30)
    paddle.checkout("txn_1", status="active", period_end=first_end, at=clock.now)
    await service.poll_once()

    clock.now = first_end + datetime.timedelta(hours=1)
    paddle.checkout("txn_2", status="active", period_end=first_end + datetime.timedelta(days=30), at=clock.now)
    await service.poll_once()

    assert (await service.access(_GUILD)).until == first_end + datetime.timedelta(days=30)
    assert announced == ["txn_1"]


async def test_an_abandoned_pending_card_row_expires(db: FakeDatabase, clock: Clock) -> None:
    paddle = FakePaddle()
    service = _service(db, clock, paddle=paddle)
    await service.access(_GUILD)
    _website_row(db, "paddle", "txn_9", months=1, amount_usd=2.99)
    paddle.transactions["txn_9"] = {"id": "txn_9", "status": "ready", "subscription_id": None, "updated_at": _START.isoformat()}
    clock.advance(days=2)

    await service.poll_once()

    assert db.tables["pulsenotify_payments"].rows[0]["status"] == "expired"


async def test_card_is_unavailable_without_a_paddle_key(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock, paddle=None)

    await service.poll_once()

    assert service.card_available is False


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

    statuses = sorted(row["status"] for row in db.tables["pulsenotify_payments"].rows)
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

    assert db.tables["pulsenotify_payments"].rows[0]["status"] == "failed"
    assert _row(db).get("paid_until") is None


# ── website links ───────────────────────────────────────────────────────


def test_website_links_carry_the_server_id(db: FakeDatabase, clock: Clock) -> None:
    service = _service(
        db, clock, subscribe_url="https://pulsenotify.duoplus.org/subscribe", cancel_url="https://x.org/c?ref=bot"
    )

    assert service.subscribe_url(42) == "https://pulsenotify.duoplus.org/subscribe?guild_id=42"
    assert service.cancel_url(42) == "https://x.org/c?ref=bot&guild_id=42"
    assert with_server("https://x.org/servers/{guild_id}/pay", 42) == "https://x.org/servers/42/pay"
    assert _service(db, clock).subscribe_url(42) is None


# ── upgrade reminders ───────────────────────────────────────────────────


async def test_reminders_start_when_the_trial_ends_then_repeat_weekly_up_to_three(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.access(_GUILD)

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
    await service.access(_GUILD)
    clock.advance(days=8)
    for row in await service.reminders_due({_GUILD}):
        await service.mark_reminded(row)

    _row(db)["paid_until"] = (clock.now + datetime.timedelta(days=30)).isoformat()
    assert await service.reminders_due({_GUILD}) == []
    assert _row(db)["reminders_sent"] == 0

    clock.advance(days=34)  # paid time + grace over
    assert [row["guild_id"] for row in await service.reminders_due({_GUILD})] == [_GUILD]


async def test_whitelisted_servers_and_billing_off_get_no_reminders(db: FakeDatabase, clock: Clock) -> None:
    service = _service(db, clock)
    await service.whitelist(_GUILD, by=1, note=None)
    clock.advance(days=30)

    assert await service.reminders_due({_GUILD}) == []
    assert await _service(db, clock, enabled=False).reminders_due({_GUILD}) == []


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


# ── price display ───────────────────────────────────────────────────────


def test_prices_display_without_needless_cents() -> None:
    assert format_usd(1) == "$1"
    assert format_usd(3.0) == "$3"
    assert format_usd(2.5) == "$2.50"


async def test_paddle_list_paid_transactions_request_shape() -> None:
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"id": "txn_1"}], "meta": {}})

    client = PaddleClient("key", transport=httpx.MockTransport(handler))

    assert await client.list_paid_transactions(updated_after="2026-10-01T12:00:00+00:00") == [{"id": "txn_1"}]
    assert seen == {
        "path": "/transactions",
        "status": "paid,completed",
        "updated_at[GT]": "2026-10-01T12:00:00+00:00",
        "order_by": "updated_at[ASC]",
        "per_page": "100",
    }


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


async def test_nowpayments_min_amount_request_shape() -> None:
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"currency_from": "usdttrc20", "min_amount": 11.16, "fiat_equivalent": 11.18})

    client = NowPaymentsClient("np-key", transport=httpx.MockTransport(handler))

    assert await client.min_amount_usd("usdttrc20") == 11.18
    assert seen == {"currency_from": "usdttrc20", "currency_to": "usdttrc20", "fiat_equivalent": "usd"}


# ── Paddle environment from the key ─────────────────────────────────────


def test_paddle_environment_comes_from_the_key() -> None:
    """Real case 2026-10-13: a live key with PADDLE_SANDBOX=true was sent to
    the sandbox and rejected."""
    assert is_sandbox_key("pdl_live_apikey_abc", configured_sandbox=True) is False
    assert is_sandbox_key("pdl_sdbx_apikey_abc", configured_sandbox=False) is True
    assert is_sandbox_key("old-style-key", configured_sandbox=True) is True
