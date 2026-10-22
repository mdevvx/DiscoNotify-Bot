"""Where billing meets the rest of the bot: the monitoring scheduler only
polls servers that may be monitored (fixes /notify toggle off never
actually pausing anything), the command gate, and the billing commands."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import pytest

from billing.access import Access, AccessReason
from bot.client import DiscoNotifyCommandTree
from commands.admin.billing import cancel, subscribe
from commands.admin.whitelist import WhitelistCog
from tests.unit.fake_interactions import FakeFollowup, FakeResponse, FakeUser
from tests.unit.fakes import FakeDatabase
from tests.unit.test_scheduler import _make_scheduler
from tests.unit.test_worker import FakeAdapter, _seed_account

_OWNER = 947795908119109652


# ── scheduler filter ────────────────────────────────────────────────────


async def test_scheduler_skips_accounts_of_servers_that_may_not_be_monitored() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1", guild_id=1)
    _seed_account(db, "acc-2", guild_id=2)
    scheduler = _make_scheduler(db, FakeAdapter(), [])
    asked: List[Set[int]] = []

    async def only_guild_1(guild_ids: Set[int]) -> Set[int]:
        asked.append(guild_ids)
        return {1}

    scheduler._guild_filter = only_guild_1
    await scheduler._sync_workers()

    assert set(scheduler._workers) == {"acc-1"}
    assert asked == [{1, 2}]


async def test_a_failing_filter_keeps_monitoring_everyone() -> None:
    """A billing/database hiccup must not cut off servers that are paying."""
    db = FakeDatabase()
    _seed_account(db, "acc-1", guild_id=1)
    scheduler = _make_scheduler(db, FakeAdapter(), [])

    async def broken(guild_ids: Set[int]) -> Set[int]:
        raise RuntimeError("database down")

    scheduler._guild_filter = broken
    await scheduler._sync_workers()

    assert set(scheduler._workers) == {"acc-1"}


async def test_a_server_that_regains_access_is_polled_again() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1", guild_id=1)
    scheduler = _make_scheduler(db, FakeAdapter(), [])
    allowed: Set[int] = set()

    async def current(guild_ids: Set[int]) -> Set[int]:
        return guild_ids & allowed

    scheduler._guild_filter = current
    await scheduler._sync_workers()
    paused = set(scheduler._workers)
    allowed.add(1)
    await scheduler._sync_workers()

    assert paused == set()
    assert set(scheduler._workers) == {"acc-1"}


# ── command gate ────────────────────────────────────────────────────────


class FakeGuildsRepo:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    async def is_enabled(self, guild_id: int) -> bool:
        return self.enabled


class FakeBilling:
    def __init__(self, allowed: bool) -> None:
        self.enabled = True
        self.price_usd = 1.0
        self.price_label = "$1"
        self._allowed = allowed
        self.card_available = True
        self.crypto_available = True
        self.whitelisted: List[int] = []
        self.reason = AccessReason.TRIAL if allowed else AccessReason.EXPIRED
        self.row: Any = None
        self.site: Optional[str] = "https://disconotify.com/crypto"
        self.checkouts: List[Dict[str, Any]] = []
        self.portal_for: List[int] = []
        self.payer: Optional[int] = 5
        self.checkout_error: Optional[Exception] = None
        self.website: Optional[str] = None  # set to simulate "subscribing happens on the website"

    def plain_subscribe_url(self) -> Any:
        return self.website or None

    def subscribe_url(self, guild_id: int) -> Any:
        return f"{self.site}?guild_id={guild_id}" if self.site else None

    async def start_card_checkout(self, guild_id: int, *, user_id: Any = None, channel_id: Any = None) -> str:
        if self.checkout_error is not None:
            raise self.checkout_error
        self.checkouts.append({"guild_id": guild_id, "user_id": user_id, "channel_id": channel_id})
        return f"https://buy.polar.sh/co_{len(self.checkouts)}"

    async def manage_card_url(self, guild_id: int) -> str:
        self.portal_for.append(guild_id)
        return "https://polar.sh/pulsenotify/portal?customer_session_token=t"

    async def can_manage_card(self, guild_id: int, user_id: int, *, server_owner_id: Any) -> bool:
        return user_id == self.payer or user_id == server_owner_id

    async def card_payer_id(self, guild_id: int) -> Any:
        return self.payer

    async def access(self, guild_id: int) -> Access:
        return Access(self._allowed, self.reason)

    async def billing_row(self, guild_id: int) -> Any:
        return self.row

    async def whitelist(self, guild_id: int, *, by: int, note: Any) -> None:
        self.whitelisted.append(guild_id)

    async def unwhitelist(self, guild_id: int) -> bool:
        if guild_id in self.whitelisted:
            self.whitelisted.remove(guild_id)
            return True
        return False

    async def list_whitelisted(self) -> List[dict]:
        return [{"guild_id": guild_id, "whitelist_note": None} for guild_id in self.whitelisted]


class FakeCommand:
    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name


class FakeMaintenance:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.message = "Upgrading the database, back in 10 minutes."


class FakeBot:
    def __init__(self, *, allowed: bool = True, enabled: bool = True, maintenance: bool = False) -> None:
        self.guilds_repo = FakeGuildsRepo(enabled)
        self.billing = FakeBilling(allowed)
        self.maintenance = FakeMaintenance(maintenance)

    async def is_owner(self, user: Any) -> bool:
        return user.id == _OWNER

    def get_guild(self, guild_id: int) -> Any:
        return None


class FakeGuild:
    id = 1
    name = "Custom Bots"


class FakeInteraction:
    def __init__(self, bot: FakeBot, command: str = "", *, user_id: int = 5) -> None:
        self.client = bot
        self.guild = FakeGuild()
        self.guild_id = 1
        self.channel_id = 555
        self.user = FakeUser(user_id)
        self.command = FakeCommand(command)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeTree:
    def __init__(self, bot: FakeBot) -> None:
        self.client = bot


async def _check(bot: FakeBot, command: str) -> "tuple[bool, FakeInteraction]":
    interaction = FakeInteraction(bot, command)
    allowed = await DiscoNotifyCommandTree.interaction_check(FakeTree(bot), interaction)  # type: ignore[arg-type]
    return allowed, interaction


async def test_a_server_without_access_is_told_to_subscribe() -> None:
    allowed, interaction = await _check(FakeBot(allowed=False), "notify account-add")

    reply = interaction.response.messages[0]
    assert allowed is False
    assert reply["ephemeral"] is True
    assert "Subscription needed" in reply["content"]
    assert "/notify subscribe" in reply["content"] and "$1/month" in reply["content"]
    assert "every setting is kept" in reply["content"]
    assert _link_urls(reply["view"]) == []  # the message points to /notify subscribe, which creates the checkout


def _link_urls(view: Any) -> List[str]:
    import discord

    return [item.url for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.url]


def test_subscription_needed_says_what_ended() -> None:
    from billing.access import subscription_needed

    trial = " ".join(subscription_needed({"trial_ends_at": "2026-10-20T13:51:56+00:00"}, price_label="$2.99"))
    paid = " ".join(
        subscription_needed(
            {"trial_ends_at": "2026-10-20T13:51:56+00:00", "paid_until": "2026-11-20T00:00:00+00:00"}, price_label="$2.99"
        )
    )

    assert "free trial ended on <t:" in trial
    assert "subscription ended on <t:" in paid
    assert "$2.99/month" in trial


# ── maintenance mode ────────────────────────────────────────────────────


async def test_maintenance_blocks_every_command_for_members() -> None:
    for command in ("notify account-add", "notify status", "notify subscribe"):
        allowed, interaction = await _check(FakeBot(maintenance=True), command)

        reply = interaction.response.messages[0]
        assert allowed is False
        assert "under maintenance" in reply["content"]
        assert "back in 10 minutes" in reply["content"]


async def test_the_owner_can_still_use_commands_during_maintenance() -> None:
    bot = FakeBot(maintenance=True)
    interaction = FakeInteraction(bot, "notify account-add", user_id=_OWNER)

    allowed = await DiscoNotifyCommandTree.interaction_check(FakeTree(bot), interaction)  # type: ignore[arg-type]

    assert allowed is True


async def test_maintenance_pauses_monitoring_everywhere() -> None:
    from bot.client import DiscoNotifyBot

    bot = FakeBot(maintenance=True)
    allowed = await DiscoNotifyBot._monitorable_guilds(bot, {1, 2, 3})  # type: ignore[arg-type]

    assert allowed == set()


async def test_maintenance_state_survives_a_restart() -> None:
    from bot.maintenance import Maintenance
    from database.repositories.bot_state import BotStateRepository

    db = FakeDatabase()
    first = Maintenance(BotStateRepository(db))  # type: ignore[arg-type]
    await first.enable(message="Back soon", by=_OWNER)

    after_restart = Maintenance(BotStateRepository(db))  # type: ignore[arg-type]
    await after_restart.load()
    assert after_restart.active and after_restart.message == "Back soon"

    await after_restart.disable()
    again = Maintenance(BotStateRepository(db))  # type: ignore[arg-type]
    await again.load()
    assert again.active is False


async def test_the_admin_panels_switch_turns_maintenance_on_and_off() -> None:
    from bot.maintenance import Maintenance
    from database.repositories.bot_state import BotStateRepository

    repo = BotStateRepository(FakeDatabase())  # type: ignore[arg-type]
    state = Maintenance(repo)

    assert await state.refresh() is False  # no switch row yet: nothing changes
    await repo.set("maintenance_mode", True)
    assert await state.refresh() is True and state.active
    assert await state.refresh() is False  # already on
    await repo.set("maintenance_mode", False)
    assert await state.refresh() is True and not state.active


async def test_maintenance_commands_keep_the_panels_switch_in_step() -> None:
    from bot.maintenance import Maintenance
    from database.repositories.bot_state import BotStateRepository

    repo = BotStateRepository(FakeDatabase())  # type: ignore[arg-type]
    state = Maintenance(repo)
    await state.enable(message=None, by=_OWNER)
    assert await repo.get("maintenance_mode") is True
    await state.disable()
    assert await repo.get("maintenance_mode") is False


async def test_a_missing_state_table_means_not_in_maintenance() -> None:
    from bot.maintenance import Maintenance

    class BrokenRepo:
        async def get(self, key: str) -> Any:
            raise RuntimeError("relation notify_bot_state does not exist")

    state = Maintenance(BrokenRepo())  # type: ignore[arg-type]
    await state.load()

    assert state.active is False


async def test_maintenance_on_asks_to_confirm_and_only_confirm_turns_it_on() -> None:
    from commands.admin.maintenance import ConfirmMaintenanceView

    class Recorder:
        def __init__(self) -> None:
            self.enabled_with: List[Any] = []
            self.presence_updates = 0

        async def enable(self, *, message: Any, by: int) -> None:
            self.enabled_with.append((message, by))

        async def apply_presence(self) -> None:
            self.presence_updates += 1

    recorder = Recorder()
    bot = type("Bot", (), {"maintenance": recorder, "apply_presence": recorder.apply_presence})()
    view = ConfirmMaintenanceView(bot, owner_id=_OWNER, message="Back soon")

    cancel = FakeInteraction(FakeBot(), user_id=_OWNER)
    await view.cancel(cancel)  # type: ignore[arg-type]
    assert recorder.enabled_with == []

    confirm = FakeInteraction(FakeBot(), user_id=_OWNER)
    await view.confirm(confirm)  # type: ignore[arg-type]
    assert recorder.enabled_with == [("Back soon", _OWNER)]
    assert recorder.presence_updates == 1
    assert "Maintenance mode is on" in confirm.response.edits[0]["content"]
    assert await view.interaction_check(FakeInteraction(FakeBot(), user_id=5)) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "command",
    [
        "notify subscribe",
        "notify cancel",
        "notify status",
        "notify help",
        "notify toggle",
        "notify global status",
    ],
)
async def test_billing_and_owner_commands_always_work(command: str) -> None:
    allowed, _ = await _check(FakeBot(allowed=False, enabled=False), command)

    assert allowed is True


async def test_a_server_with_access_can_use_everything() -> None:
    allowed, _ = await _check(FakeBot(allowed=True), "notify account-add")

    assert allowed is True


# ── billing commands ────────────────────────────────────────────────────


async def test_subscribe_says_free_while_billing_is_off() -> None:
    bot = FakeBot()
    bot.billing.enabled = False
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    assert "free" in interaction.response.messages[0]["content"].lower()


async def test_subscribe_without_the_website_never_creates_a_checkout_and_links_crypto() -> None:
    bot = FakeBot()
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    reply = (interaction.response.messages or interaction.followup.messages)[0]
    assert reply["ephemeral"] is True
    assert _link_urls(reply["view"]) == ["https://disconotify.com/crypto?guild_id=1"]
    assert bot.billing.checkouts == []  # a direct Polar checkout link is never handed out


async def test_subscribe_in_website_mode_gives_one_plain_link_and_creates_no_checkout() -> None:
    bot = FakeBot()
    bot.billing.website = "https://disconotify.com/subscribe"
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    assert _link_urls(reply["view"]) == ["https://disconotify.com/subscribe"]
    assert bot.billing.checkouts == []  # the website creates the checkout
    assert "website" in reply["content"]


async def test_website_mode_adds_no_second_subscription_button_for_a_running_card_subscription() -> None:
    bot = FakeBot()
    bot.billing.website = "https://disconotify.com/subscribe"
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "active", "card_customer_id": "cus_1"}
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    assert _link_urls(interaction.response.messages[0]["view"]) == []


async def test_subscribe_without_card_or_crypto_set_up_says_it_isnt_ready() -> None:
    bot = FakeBot()
    bot.billing.card_available = False
    bot.billing.site = None
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert _link_urls(reply["view"]) == []
    assert "aren't switched on yet" in reply["content"]


async def test_a_server_with_a_running_card_subscription_gets_no_second_checkout() -> None:
    """Two checkouts would mean two subscriptions and a double charge."""
    bot = FakeBot()
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "active", "card_customer_id": "cus_1"}
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    assert bot.billing.checkouts == []
    assert "already set up" in interaction.response.messages[0]["content"]


async def test_a_billing_page_polar_cant_open_is_explained_without_jargon() -> None:
    from billing.service import BillingUnavailable

    bot = FakeBot()
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "trialing", "card_customer_id": "cus_sandbox"}

    async def refuse(guild_id: int) -> str:
        raise BillingUnavailable(
            "We couldn't open the billing page for this server's subscription. Please contact support and we'll sort it out."
        )

    bot.billing.manage_card_url = refuse  # type: ignore[method-assign]
    interaction = FakeInteraction(bot, user_id=5)

    await cancel.callback(interaction)  # type: ignore[arg-type]

    text = (interaction.response.messages or interaction.followup.messages)[-1]["content"]
    assert "couldn't open the billing page" in text and "contact support" in text
    assert "Polar" not in text and "422" not in text


async def test_cancel_opens_the_billing_portal_for_the_payer() -> None:
    bot = FakeBot()
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "active", "card_customer_id": "cus_1"}
    interaction = FakeInteraction(bot, user_id=5)  # 5 paid

    await cancel.callback(interaction)  # type: ignore[arg-type]

    reply = (interaction.response.messages or interaction.followup.messages)[0]
    assert reply["ephemeral"] is True
    assert _link_urls(reply["view"]) == ["https://polar.sh/pulsenotify/portal?customer_session_token=t"]
    assert "stays active until the end of the month" in reply["content"]
    assert bot.billing.portal_for == [1]


async def test_cancel_refuses_the_portal_to_an_admin_who_didnt_pay_and_isnt_the_owner() -> None:
    """The link is pre-authenticated as the payer and shows their email and card."""
    bot = FakeBot()
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "active", "card_customer_id": "cus_1"}
    interaction = FakeInteraction(bot, user_id=77)

    await cancel.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert _link_urls(reply["view"]) == [] and bot.billing.portal_for == []
    assert "only <@5> (who subscribed) or the server owner can open it" in reply["content"]


async def test_the_server_owner_can_always_open_the_portal() -> None:
    bot = FakeBot()
    bot.billing.reason = AccessReason.PAID
    bot.billing.row = {"card_status": "active", "card_customer_id": "cus_1"}
    interaction = FakeInteraction(bot, user_id=77)
    interaction.guild.owner_id = 77  # type: ignore[attr-defined]

    await cancel.callback(interaction)  # type: ignore[arg-type]

    assert bot.billing.portal_for == [1]


async def test_a_cooldown_gets_a_readable_message() -> None:
    from discord import app_commands

    from bot.events import handle_app_command_error

    interaction = FakeInteraction(FakeBot())
    cooldown = app_commands.Cooldown(3, 30.0)

    await handle_app_command_error(interaction, app_commands.CommandOnCooldown(cooldown, 12.4))  # type: ignore[arg-type]

    assert "you can use this again in 12 seconds" in interaction.response.messages[0]["content"]


@pytest.mark.parametrize(
    ("reason", "row", "expected"),
    [
        (AccessReason.TRIAL, {"card_status": "trialing"}, "hasn't been charged"),
        (AccessReason.PAID, {"card_status": "active", "card_cancels_at": "2026-11-01T00:00:00+00:00"}, "Already cancelled"),
        (AccessReason.PAID, {}, "prepaid with crypto"),
        (AccessReason.TRIAL, {}, "free trial with no payment method"),
        (AccessReason.WHITELISTED, {}, "whitelisted"),
    ],
)
async def test_cancel_explains_what_cancelling_means_for_this_server(reason: AccessReason, row: Any, expected: str) -> None:
    bot = FakeBot()
    bot.billing.reason = reason
    bot.billing.row = row
    interaction = FakeInteraction(bot)

    await cancel.callback(interaction)  # type: ignore[arg-type]

    assert expected in interaction.response.messages[0]["content"]


# ── upgrade reminder DMs ────────────────────────────────────────────────


class _Recipient:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def send(self, *, view: Any = None, allowed_mentions: Any = None) -> None:
        self.sent.append(view)


class _DmLog:
    def __init__(self, already: Any = ()) -> None:
        self.rows: List[Any] = []
        self._already = set(already)

    async def add(self, **row: Any) -> None:
        self.rows.append(row)

    async def was_sent(self, payment_id: str, kind: str) -> bool:
        return (payment_id, kind) in self._already


def _discord_error(code: int, status: int = 403) -> Exception:
    import discord

    response = type("R", (), {"status": status, "reason": "x"})()
    return discord.HTTPException(response, {"code": code, "message": "nope"})


def _dm_bot(recipient: Any, dm_log: Any) -> Any:
    from bot.client import DiscoNotifyBot

    bot = type("B", (), {})()
    bot.get_user = lambda user_id: recipient
    bot.dm_log = dm_log
    bot._dm_user = DiscoNotifyBot._dm_user.__get__(bot)
    return bot


async def test_a_dm_to_a_member_with_closed_dms_is_logged_as_dm_closed() -> None:
    class Closed(_Recipient):
        async def send(self, **_: Any) -> None:
            raise _discord_error(50007)

    log = _DmLog()
    delivered = await _dm_bot(Closed(), log)._dm_user(5, object(), guild_id=1, kind="payment_failed", payment_id="p1")

    assert delivered is False
    assert log.rows == [
        {"guild_id": 1, "user_id": 5, "kind": "payment_failed", "status": "dm_closed", "error": None, "payment_id": "p1"}
    ]


async def test_any_other_dm_error_is_logged_as_failed_with_a_short_reason() -> None:
    class Broken(_Recipient):
        async def send(self, **_: Any) -> None:
            raise _discord_error(0, status=500)

    log = _DmLog()
    await _dm_bot(Broken(), log)._dm_user(5, object(), guild_id=1, kind="payment_success")

    assert log.rows[0]["status"] == "failed" and "500" in log.rows[0]["error"]


async def test_a_dm_still_goes_out_when_the_log_cant_be_written() -> None:
    class Down(_DmLog):
        async def add(self, **row: Any) -> None:
            raise RuntimeError("relation notify_dm_log does not exist")

    recipient = _Recipient()
    assert await _dm_bot(recipient, Down())._dm_user(5, object(), guild_id=1, kind="payment_success") is True
    assert len(recipient.sent) == 1


async def test_an_abandoned_checkout_is_dmed_once_and_never_again() -> None:
    from bot.client import DiscoNotifyBot

    payments = [
        {"id": "p-new", "guild_id": 1, "requested_by": 5},
        {"id": "p-done", "guild_id": 1, "requested_by": 6},
    ]

    class Billing(FakeBilling):
        async def abandoned_checkouts(self) -> List[Any]:
            return payments

    buyer = _Recipient()
    log = _DmLog(already={("p-done", "checkout_abandoned")})
    bot = _dm_bot(buyer, log)
    bot.billing = Billing(allowed=False)
    bot.billing.website = None
    bot.get_guild = lambda guild_id: type("G", (), {"id": guild_id, "name": "Gaming Hub"})()
    bot.send_abandoned_checkout_reminders = DiscoNotifyBot.send_abandoned_checkout_reminders.__get__(bot)

    await bot.send_abandoned_checkout_reminders()

    assert len(buyer.sent) == 1
    assert [(row["user_id"], row["kind"], row["payment_id"]) for row in log.rows] == [(5, "checkout_abandoned", "p-new")]


async def test_the_server_owner_is_dmed_an_upgrade_reminder_with_the_link() -> None:
    from bot.client import DiscoNotifyBot
    from ui.cards import plain_text

    owner = _Recipient()
    guild = type("G", (), {"id": 1, "name": "Gaming Hub", "owner_id": 9, "owner": owner})()
    marked: List[Any] = []
    row = {"guild_id": 1, "trial_ends_at": "2026-10-08T00:00:00+00:00", "reminders_sent": 0}

    class Billing(FakeBilling):
        async def reminders_due(self, guild_ids: Any) -> List[Any]:
            assert set(guild_ids) == {1}
            return [row]

        async def mark_reminded(self, due: Any) -> None:
            marked.append(due["guild_id"])

    bot = type("B", (), {})()
    bot.guilds = [guild]
    bot.billing = Billing(allowed=False)
    bot.settings = type("S", (), {"billing_reminder_max": 3})()
    bot.billing.website = None
    bot.get_user = lambda user_id: owner
    bot.dm_log = _DmLog()
    for name in ("send_upgrade_reminders", "_reminder_card", "_dm_owner", "_dm_user"):
        setattr(bot, name, getattr(DiscoNotifyBot, name).__get__(bot))

    await bot.send_upgrade_reminders()

    assert bot.dm_log.rows == [
        {"guild_id": 1, "user_id": 9, "kind": "expiry_reminder", "status": "sent", "error": None, "payment_id": None}
    ]
    text = plain_text(owner.sent[0])
    assert "trial has ended" in text and "Gaming Hub" in text
    assert _link_urls(owner.sent[0]) == ["https://disconotify.com/crypto?guild_id=1"]
    assert bot.billing.checkouts == []
    assert marked == [1]


# ── !whitelist (owner-only text command) ────────────────────────────────


class FakeCtx:
    """Just what WhitelistCog uses from commands.Context."""

    def __init__(self, guild_id: int = 1) -> None:
        self.guild = type("G", (), {"id": guild_id})()
        self.author = FakeUser(_OWNER)
        self.clean_prefix = "!"
        self.sent: List[str] = []

    async def send(self, *, view: Any = None) -> None:
        from ui.cards import plain_text

        self.sent.append(plain_text(view))


async def test_whitelist_is_a_text_command_not_in_the_slash_menu() -> None:
    """Discord can't hide a slash subcommand from individual users, so the
    owner's whitelist must not be one."""
    from commands.notify_group import notify_group
    from discord import app_commands

    import commands.admin.billing  # noqa: F401  (registers /notify subscribe)

    names = [c.qualified_name for c in notify_group.walk_commands() if isinstance(c, app_commands.Command)]
    assert not any("whitelist" in name for name in names)


async def test_whitelist_commands_are_owner_only() -> None:
    cog = WhitelistCog(FakeBot())  # type: ignore[arg-type]
    for command in (cog.whitelist, cog.add, cog.remove, cog.list_):
        assert any("is_owner" in getattr(check, "__qualname__", "") for check in command.checks)


async def test_owner_can_whitelist_this_server_or_another_by_id() -> None:
    bot = FakeBot()
    cog = WhitelistCog(bot)  # type: ignore[arg-type]

    here = FakeCtx(guild_id=1)
    await cog.add.callback(cog, here, None, note=None)  # type: ignore[arg-type]
    other = FakeCtx()
    await cog.add.callback(cog, other, "1466009969806545009", note="friend")  # type: ignore[arg-type]
    listing = FakeCtx()
    await cog.list_.callback(cog, listing)  # type: ignore[arg-type]

    assert bot.billing.whitelisted == [1, 1466009969806545009]
    assert "1466009969806545009" in listing.sent[0]


async def test_whitelist_add_without_an_id_treats_the_words_as_the_note() -> None:
    bot = FakeBot()
    cog = WhitelistCog(bot)  # type: ignore[arg-type]

    await cog.add.callback(cog, FakeCtx(guild_id=7), "my", note="own server")  # type: ignore[arg-type]

    assert bot.billing.whitelisted == [7]


async def test_whitelist_remove_reports_a_server_that_was_not_whitelisted() -> None:
    bot = FakeBot()
    cog = WhitelistCog(bot)  # type: ignore[arg-type]
    ctx = FakeCtx()

    await cog.remove.callback(cog, ctx, "123")  # type: ignore[arg-type]

    assert "isn't whitelisted" in ctx.sent[0]


# ── skipping accounts that can't send any alert ─────────────────────────


async def test_accounts_with_every_alert_off_are_not_polled() -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-on", guild_id=1)
    _seed_account(db, "acc-off", guild_id=1)
    scheduler = _make_scheduler(db, FakeAdapter(), [])

    async def muted(account_ids: Set[str]) -> Set[str]:
        return {"acc-off"} & account_ids

    scheduler._muted_accounts = muted
    await scheduler._sync_workers()

    assert set(scheduler._workers) == {"acc-on"}


async def test_every_alert_off_means_configs_exist_and_none_are_on() -> None:
    from database.repositories.alert_configs import AlertConfigurationRepository

    db = FakeDatabase()
    repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]
    await repo.seed_defaults(1, "all-off", ["video_published", "short_published"])
    await repo.seed_defaults(1, "one-on", ["video_published", "short_published"])
    for row in db.tables["notify_alert_configurations"].rows:
        if row["account_id"] == "all-off" or row["event_type"] == "short_published":
            row["enabled"] = False

    muted = await repo.accounts_with_every_alert_off({"all-off", "one-on", "no-settings"})

    assert muted == {"all-off"}  # "no-settings" has no rows: the sender treats that as alerts on
