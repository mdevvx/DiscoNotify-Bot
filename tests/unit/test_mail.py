"""Email: template rendering, unsubscribe tokens, the outbox (queueing,
sending, retries, skipping) and the automatic emails (trial-ended and
payment-coming-up reminders, billing events) — driven through a fake email
provider and the fake database, with an injectable clock."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pytest

from billing.access import Access, AccessReason
from billing.links import sign_link, sign_unsubscribe, verify_link, verify_unsubscribe
from billing.repository import BillingRepository
from billing.service import BillingEvent, BillingService
from database.repositories.bot_state import BotStateRepository
from mail.render import render
from mail.repository import MailRepository
from mail.errors import PermanentMailError, TransientMailError
from mail.service import EmailService
from tests.unit.fakes import FakeDatabase

_GUILD = 1
_START = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
_SECRET = "s3cret-" * 8


class Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += datetime.timedelta(**kwargs)


class FakeMailClient:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.error: Optional[Exception] = None

    async def send(self, **message: Any) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(message)
        return f"msg_{len(self.sent)}"

    async def aclose(self) -> None:
        pass


def _template(key: str, *, category: str = "transactional", enabled: bool = True, **fields: Any) -> Dict[str, Any]:
    return {
        "key": key,
        "name": key,
        "category": category,
        "enabled": enabled,
        "subject": fields.get("subject", "Hello {{server_name}}"),
        "preheader": fields.get("preheader", "pre {{server_name}}"),
        "body_html": fields.get("body_html", "<p>Hi {{server_name}} <a href=\"{{{subscribe_url}}}\">go</a></p>"),
        "body_text": fields.get("body_text", "Hi {{server_name}} {{subscribe_url}}"),
    }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    database.tables["notify_guilds"].rows.append({"guild_id": _GUILD, "enabled": True})
    for key, category in (
        ("trial_activated", "transactional"),
        ("subscription_started", "transactional"),
        ("subscription_cancelled", "transactional"),
        ("payment_failed", "transactional"),
        ("renewal_reminder", "transactional"),
        ("trial_ended_reminder", "marketing"),
        ("subscription_renewed", "transactional"),
        ("subscription_ended", "transactional"),
        ("subscription_lapsed_reminder", "marketing"),
    ):
        database.tables["notify_email_templates"].rows.append(_template(key, category=category))
    return database


@pytest.fixture
def client() -> FakeMailClient:
    return FakeMailClient()


def _service(db: FakeDatabase, clock: Clock, client: Optional[FakeMailClient], **kwargs: Any) -> EmailService:
    defaults: Dict[str, Any] = {
        "client": client,
        "link_secret": _SECRET,
        "unsubscribe_url": "https://disconotify.com/unsubscribe",
        "subscribe_url": "https://disconotify.com/subscribe",
        "activate_url": "https://disconotify.com/activate",
        "manage_url": "https://disconotify.com/manage",
        "support_email": "support@disconotify.com",
        "price_label": "$1.99",
        "clock": clock,
    }
    defaults.update(kwargs)
    return EmailService(MailRepository(db), BotStateRepository(db), **defaults)  # type: ignore[arg-type]


def _billing(db: FakeDatabase, clock: Clock) -> BillingService:
    return BillingService(BillingRepository(db), enabled=True, clock=clock)  # type: ignore[arg-type]


def _contact(db: FakeDatabase, email: str = "owner@example.com", *, unsubscribed: bool = False, guild_id: int = _GUILD) -> None:
    db.tables["notify_email_contacts"].rows.append(
        {
            "id": f"c-{email}-{guild_id}",
            "guild_id": guild_id,
            "email": email,
            "marketing_unsubscribed_at": "2026-09-01T00:00:00+00:00" if unsubscribed else None,
        }
    )


def _outbox(db: FakeDatabase) -> List[Dict[str, Any]]:
    return db.tables["notify_email_outbox"].rows


# ── rendering ───────────────────────────────────────────────────────────


def test_variables_are_substituted_and_html_is_escaped() -> None:
    rendered = render(_template("x"), {"server_name": "<b>Hub</b> & co", "subscribe_url": "https://x.test/s?a=1&b=2"})

    assert rendered.subject == "Hello <b>Hub</b> & co"  # the subject is plain text
    assert "&lt;b&gt;Hub&lt;/b&gt; &amp; co" in rendered.html
    assert 'href="https://x.test/s?a=1&b=2"' in rendered.html  # triple braces: a link, unescaped
    assert "Hi <b>Hub</b> & co https://x.test/s?a=1&b=2" in rendered.text


def test_triple_braces_never_unescape_anything_but_links() -> None:
    template = _template("x", body_html="{{{server_name}}}")

    assert "&lt;script&gt;" in render(template, {"server_name": "<script>"}).html


def test_unknown_variables_are_blank_and_reported() -> None:
    rendered = render(_template("x", subject="A {{nope}} B"), {})

    assert rendered.subject == "A B" and rendered.missing == ["nope"]


def test_a_subject_is_always_one_line() -> None:
    assert render(_template("x", subject="a\nb\r\nc"), {}).subject == "a b c"


def test_marketing_emails_get_an_unsubscribe_footer_only_when_a_link_is_given() -> None:
    plain = render(_template("x"), {})
    with_link = render(_template("x"), {"unsubscribe_url": "https://x.test/u?t=1"})

    assert "Unsubscribe" not in plain.html
    assert "https://x.test/u?t=1" in with_link.html and "https://x.test/u?t=1" in with_link.text


def test_the_layout_wraps_the_body_with_the_preheader() -> None:
    rendered = render(_template("x"), {"server_name": "Hub"}, {"brand_name": "Acme", "logo_url": ""})

    assert "pre Hub" in rendered.html and "Acme" in rendered.html and rendered.html.startswith("<!doctype html>")


# ── unsubscribe tokens ──────────────────────────────────────────────────


def test_an_unsubscribe_token_round_trips_and_is_tamper_proof() -> None:
    token = sign_unsubscribe(_SECRET, email="A@Example.com", guild_id=5, now=1000.0)

    payload = verify_unsubscribe(_SECRET, token, now=2000.0)
    assert payload is not None and payload["e"] == "a@example.com" and payload["g"] == "5"
    assert verify_unsubscribe("other-secret", token, now=2000.0) is None
    assert verify_unsubscribe(_SECRET, token + "x", now=2000.0) is None
    assert verify_unsubscribe(_SECRET, token, now=1000.0 + 366 * 86400) is None  # a year later


def test_a_subscribe_token_is_not_an_unsubscribe_token_and_vice_versa() -> None:
    subscribe = sign_link(_SECRET, guild_id=1, now=1000.0)
    unsubscribe = sign_unsubscribe(_SECRET, email="a@example.com", now=1000.0)

    assert verify_unsubscribe(_SECRET, subscribe, now=1001.0) is None
    assert verify_link(_SECRET, unsubscribe, now=1001.0) is None


# ── queueing ────────────────────────────────────────────────────────────


async def test_an_email_is_queued_for_every_contact_of_the_server(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db, "a@example.com")
    _contact(db, "b@example.com")
    _contact(db, "other@example.com", guild_id=2)

    queued = await _service(db, clock, client).queue("payment_failed", _GUILD, {"server_name": "Hub"}, dedupe_key="pf:1")

    assert queued == 2
    assert sorted(row["to_email"] for row in _outbox(db)) == ["a@example.com", "b@example.com"]


async def test_the_same_email_is_never_queued_twice(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)

    assert await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf:1") == 1
    assert await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf:1") == 0
    assert len(_outbox(db)) == 1


async def test_nothing_is_queued_without_contacts_or_for_a_switched_off_or_missing_template(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    service = _service(db, clock, client)
    assert await service.queue("payment_failed", _GUILD, {}, dedupe_key="a") == 0  # no contacts

    _contact(db)
    db.tables["notify_email_templates"].rows[3]["enabled"] = False  # payment_failed
    assert await service.queue("payment_failed", _GUILD, {}, dedupe_key="b") == 0
    assert await service.queue("no_such_template", _GUILD, {}, dedupe_key="c") == 0
    assert _outbox(db) == []


async def test_marketing_emails_skip_people_who_unsubscribed_but_billing_emails_do_not(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    _contact(db, "gone@example.com", unsubscribed=True)
    _contact(db, "here@example.com")
    service = _service(db, clock, client)

    assert await service.queue("trial_ended_reminder", _GUILD, {}, dedupe_key="t") == 1
    assert await service.queue("payment_failed", _GUILD, {}, dedupe_key="p") == 2
    assert [row["to_email"] for row in _outbox(db) if row["template_key"] == "trial_ended_reminder"] == ["here@example.com"]


async def test_dates_are_stored_as_text(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)

    await _service(db, clock, client).queue("renewal_reminder", _GUILD, {"next_charge_date": _START}, dedupe_key="r")

    assert _outbox(db)[0]["variables"]["next_charge_date"] == _START.isoformat()


# ── sending ─────────────────────────────────────────────────────────────


async def test_queued_emails_are_sent_and_logged(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("payment_failed", _GUILD, {"server_name": "Hub"}, dedupe_key="pf")

    assert await service.process_outbox() == 1

    message = client.sent[0]
    assert message["to"] == "owner@example.com" and message["subject"] == "Hello Hub"
    assert "https://disconotify.com/subscribe" in message["html"]
    assert message["unsubscribe_url"] is None  # a billing email
    row = _outbox(db)[0]
    assert row["status"] == "sent" and row["provider_id"] == "msg_1" and row["subject_sent"] == "Hello Hub"
    assert await service.process_outbox() == 0  # nothing is sent twice


async def test_a_marketing_email_carries_a_working_unsubscribe_link(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("trial_ended_reminder", _GUILD, {"server_name": "Hub"}, dedupe_key="t")

    await service.process_outbox()

    url = client.sent[0]["unsubscribe_url"]
    assert url.startswith("https://disconotify.com/unsubscribe?t=")
    payload = verify_unsubscribe(_SECRET, url.split("t=", 1)[1], now=clock.now.timestamp())
    assert payload is not None and payload["e"] == "owner@example.com" and payload["g"] == "1"
    assert url in client.sent[0]["html"]


async def test_a_marketing_email_to_someone_who_unsubscribed_meanwhile_is_skipped(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("trial_ended_reminder", _GUILD, {}, dedupe_key="t")
    db.tables["notify_email_contacts"].rows[0]["marketing_unsubscribed_at"] = "2026-10-01T00:00:00+00:00"

    await service.process_outbox()

    assert client.sent == [] and _outbox(db)[0]["status"] == "skipped" and _outbox(db)[0]["status_detail"] == "unsubscribed"


async def test_an_automatic_email_whose_template_was_switched_off_is_skipped(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf")
    db.tables["notify_email_templates"].rows[3]["enabled"] = False

    await service.process_outbox()

    assert client.sent == [] and _outbox(db)[0]["status"] == "skipped"


async def test_a_temporary_failure_is_retried_later_then_given_up_on(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf")
    client.error = TransientMailError("The mail server answered 451")

    await service.process_outbox()
    assert _outbox(db)[0]["status"] == "queued" and _outbox(db)[0]["attempts"] == 1

    await service.process_outbox()  # not due yet
    assert _outbox(db)[0]["attempts"] == 1

    for _ in range(2):
        clock.advance(hours=1)
        await service.process_outbox()
    assert _outbox(db)[0]["status"] == "failed" and _outbox(db)[0]["attempts"] == 3

    client.error = None
    clock.advance(days=1)
    await service.process_outbox()
    assert client.sent == []  # a failed email is not retried forever


async def test_a_refused_email_fails_at_once(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    service = _service(db, clock, client)
    await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf")
    client.error = PermanentMailError("The mail server answered 550: no such user")

    await service.process_outbox()

    assert _outbox(db)[0]["status"] == "failed" and "550" in _outbox(db)[0]["status_detail"]


async def test_nothing_is_sent_and_nothing_is_lost_without_an_email_provider(db: FakeDatabase, clock: Clock) -> None:
    _contact(db)
    service = _service(db, clock, None)
    await service.queue("payment_failed", _GUILD, {}, dedupe_key="pf")

    assert service.enabled is False and await service.process_outbox() == 0
    assert _outbox(db)[0]["status"] == "queued"


async def test_a_manual_email_can_use_a_template_with_a_new_subject_or_be_fully_custom(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    db.tables["notify_email_outbox"].rows += [
        {"id": "m1", "to_email": "a@example.com", "template_key": "payment_failed", "kind": "manual", "status": "queued", "guild_id": _GUILD,
         "variables": {"server_name": "Hub"}, "subject_override": "Custom {{server_name}}", "send_after": _START.isoformat()},
        {"id": "m2", "to_email": "b@example.com", "template_key": None, "kind": "manual", "status": "queued", "guild_id": _GUILD,
         "variables": {"server_name": "Hub"}, "subject_override": "Hello", "body_html": "<p>Just {{server_name}}</p>",
         "body_text": "Just {{server_name}}", "send_after": _START.isoformat()},
        {"id": "m3", "to_email": "c@example.com", "template_key": None, "kind": "manual", "status": "queued", "guild_id": _GUILD,
         "variables": {}, "send_after": _START.isoformat()},
    ]
    service = _service(db, clock, client)

    assert await service.process_outbox() == 2

    subjects = {message["to"]: message["subject"] for message in client.sent}
    assert subjects == {"a@example.com": "Custom Hub", "b@example.com": "Hello"}
    assert "Just Hub" in next(m for m in client.sent if m["to"] == "b@example.com")["html"]
    assert {row["id"]: row["status"] for row in _outbox(db)} == {"m1": "sent", "m2": "sent", "m3": "skipped"}


async def test_a_manual_email_to_a_disabled_template_is_still_sent(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    db.tables["notify_email_templates"].rows[3]["enabled"] = False
    db.tables["notify_email_outbox"].rows.append(
        {"id": "m1", "to_email": "a@example.com", "template_key": "payment_failed", "kind": "manual", "status": "queued", "guild_id": _GUILD,
         "variables": {}, "send_after": _START.isoformat()}
    )

    assert await _service(db, clock, client).process_outbox() == 1


async def test_the_website_activation_email_gets_its_dates_formatted(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    """The database function queues the raw timestamp; it's formatted when sent."""
    db.tables["notify_email_templates"].rows[0]["subject"] = "Trial until {{trial_ends_at}}"
    db.tables["notify_email_outbox"].rows.append(
        {"id": "t1", "to_email": "a@example.com", "template_key": "trial_activated", "kind": "auto", "status": "queued", "guild_id": _GUILD,
         "variables": {"trial_days": 7, "trial_ends_at": "2026-10-29T12:00:00+00:00"}, "send_after": _START.isoformat()}
    )

    await _service(db, clock, client).process_outbox()

    assert client.sent[0]["subject"] == "Trial until October 29, 2026"


# ── billing events and payments ─────────────────────────────────────────


async def test_billing_events_that_matter_are_emailed_and_internal_ones_are_not(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    _contact(db)
    service = _service(db, clock, client)
    paid_until = _START + datetime.timedelta(days=30)

    assert await service.on_billing_event(BillingEvent("card_failed", _GUILD), "Hub") == 1
    assert await service.on_billing_event(BillingEvent("card_failed", _GUILD), "Hub") == 0  # same day: once
    assert await service.on_billing_event(BillingEvent("card_cancel_scheduled", _GUILD, until=_START), "Hub") == 1
    assert await service.on_billing_event(BillingEvent("card_renewed", _GUILD, until=paid_until), "Hub") == 1
    assert await service.on_billing_event(BillingEvent("card_renewed", _GUILD, until=paid_until), "Hub") == 0  # same period
    assert await service.on_billing_event(BillingEvent("card_ended", _GUILD, until=_START), "Hub") == 1
    for kind in ("crypto_failed", "crypto_expired"):  # no customer to email: nothing was paid
        assert await service.on_billing_event(BillingEvent(kind, _GUILD), "Hub") == 0
    assert sorted(row["template_key"] for row in _outbox(db)) == [
        "payment_failed",
        "subscription_cancelled",
        "subscription_ended",
        "subscription_renewed",
    ]
    renewed = next(row for row in _outbox(db) if row["template_key"] == "subscription_renewed")
    assert renewed["variables"]["status_line"] == "Your subscription is active until October 31, 2026."


async def test_a_new_subscription_email_says_when_the_trial_ends_or_the_subscription_runs_until(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    _contact(db)
    service = _service(db, clock, client)
    until = _START + datetime.timedelta(days=30)

    await service.on_payment({"id": "p1", "guild_id": _GUILD, "provider": "polar"}, Access(True, AccessReason.TRIAL, until), {"card_status": "trialing"}, "Hub")
    await service.on_payment({"id": "p2", "guild_id": _GUILD, "provider": "nowpayments"}, Access(True, AccessReason.PAID, until), {}, "Hub")

    lines = [row["variables"]["status_line"] for row in sorted(_outbox(db), key=lambda row: row["dedupe_key"])]
    assert "free trial continues until October 31, 2026" in lines[0] and "$1.99" in lines[0]
    assert lines[1] == "Your subscription is active until October 31, 2026."


# ── scheduled reminders ─────────────────────────────────────────────────


async def _activated_billing(db: FakeDatabase, clock: Clock) -> BillingService:
    billing = _billing(db, clock)
    await billing.access(_GUILD)
    row = db.tables["notify_billing"].rows[0]
    row["trial_ends_at"] = (clock.now + datetime.timedelta(days=7)).isoformat()
    row["trial_activated_at"] = clock.now.isoformat()
    return billing


async def test_the_trial_ended_reminder_is_emailed_three_times_a_week_apart(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    billing = await _activated_billing(db, clock)
    service = _service(db, clock, client, reminder_every_days=7, reminder_max=3)
    names = {_GUILD: "Hub"}

    async def run() -> int:
        return await service.send_lapse_reminders(billing, names)

    assert await run() == 0  # trial still running
    clock.advance(days=7, minutes=1)
    assert await run() == 1
    assert await run() == 0  # not twice in a row
    for _ in range(2):
        clock.advance(days=7)
        assert await run() == 1
    clock.advance(days=30)
    assert await run() == 0

    numbers = sorted(row["variables"]["reminder_number"] for row in _outbox(db))
    assert numbers == [1, 2, 3] and _outbox(db)[0]["variables"]["reminder_total"] == 3


async def test_a_server_without_an_email_is_not_counted_so_it_is_reminded_once_it_has_one(
    db: FakeDatabase, clock: Clock, client: FakeMailClient
) -> None:
    billing = await _activated_billing(db, clock)
    service = _service(db, clock, client)
    clock.advance(days=8)

    assert await service.send_lapse_reminders(billing, {_GUILD: "Hub"}) == 0
    assert db.tables["notify_billing"].rows[0].get("reminders_sent", 0) == 0

    _contact(db)
    assert await service.send_lapse_reminders(billing, {_GUILD: "Hub"}) == 1


async def test_a_server_whose_contacts_all_unsubscribed_moves_on_without_emails(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db, unsubscribed=True)
    billing = await _activated_billing(db, clock)
    service = _service(db, clock, client)
    clock.advance(days=8)

    assert await service.send_lapse_reminders(billing, {_GUILD: "Hub"}) == 0
    assert db.tables["notify_billing"].rows[0]["reminders_sent"] == 1


async def test_the_admin_panel_schedule_overrides_the_bots_configuration(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    db.tables["notify_bot_state"].rows.append(
        {"key": "email_schedule", "value": {"trial_reminder_every_days": 2, "trial_reminder_max": 1, "renewal_days_before": 5}}
    )
    service = _service(db, clock, client)

    assert await service.schedule() == {"renewal_days_before": 5, "trial_reminder_every_days": 2, "trial_reminder_max": 1}
    assert await _service(db, clock, client).layout() is not None


async def test_a_nonsense_schedule_falls_back_to_the_configuration(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    db.tables["notify_bot_state"].rows.append(
        {"key": "email_schedule", "value": {"trial_reminder_every_days": "soon", "trial_reminder_max": -1, "renewal_days_before": True}}
    )

    assert await _service(db, clock, client, renewal_days=3, reminder_every_days=7, reminder_max=3).schedule() == {
        "renewal_days_before": 3,
        "trial_reminder_every_days": 7,
        "trial_reminder_max": 3,
    }


async def test_the_payment_coming_up_email_goes_out_once_per_charge(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    billing = _billing(db, clock)
    await billing.access(_GUILD)
    charge = clock.now + datetime.timedelta(days=2)
    db.tables["notify_billing"].rows[0].update(
        {"card_subscription_id": "sub_1", "card_status": "active", "paid_until": charge.isoformat()}
    )
    service = _service(db, clock, client, renewal_days=3)
    names = {_GUILD: "Hub"}

    assert await service.send_renewal_reminders(billing, names) == 1
    assert await service.send_renewal_reminders(billing, names) == 0  # same charge: once
    assert _outbox(db)[0]["template_key"] == "renewal_reminder"

    clock.advance(days=30)
    db.tables["notify_billing"].rows[0]["paid_until"] = (clock.now + datetime.timedelta(days=2)).isoformat()
    assert await service.send_renewal_reminders(billing, names) == 1  # next month's charge


async def test_the_payment_coming_up_email_can_be_switched_off_with_zero_days(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    billing = _billing(db, clock)
    await billing.access(_GUILD)
    db.tables["notify_billing"].rows[0].update(
        {"card_subscription_id": "sub_1", "card_status": "active", "paid_until": (clock.now + datetime.timedelta(days=1)).isoformat()}
    )

    assert await _service(db, clock, client, renewal_days=0).send_renewal_reminders(billing, {_GUILD: "Hub"}) == 0


async def test_a_lapsed_paid_subscription_gets_its_own_reminder_series(db: FakeDatabase, clock: Clock, client: FakeMailClient) -> None:
    _contact(db)
    billing = _billing(db, clock)
    await billing.access(_GUILD)
    db.tables["notify_billing"].rows[0].update({"paid_until": (clock.now + datetime.timedelta(days=30)).isoformat()})
    service = _service(db, clock, client, reminder_every_days=7, reminder_max=3)
    names = {_GUILD: "Hub"}

    clock.advance(days=30, hours=1)
    assert await service.send_lapse_reminders(billing, names) == 0  # still in the grace days
    clock.advance(days=3)
    assert await service.send_lapse_reminders(billing, names) == 1

    row = _outbox(db)[0]
    assert row["template_key"] == "subscription_lapsed_reminder" and row["variables"]["reminder_number"] == 1
    clock.advance(days=7)
    assert await service.send_lapse_reminders(billing, names) == 1


def test_the_default_footer_says_replies_are_not_monitored_and_points_to_support() -> None:
    rendered = render(_template("x"), {"support_email": "support@disconotify.com"})

    assert "replies aren't monitored" in rendered.html and "support@disconotify.com" in rendered.html
    assert "replies aren't monitored" in rendered.text and "support@disconotify.com" in rendered.text
