"""/notify status (this server only), /notify global status
(bot owners only), and /notify help listing commands nested in the
"global" subcommand group."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pytest

from billing.repository import BillingRepository
from billing.service import BillingService
from commands.admin.help import help_command
from commands.admin.status import format_account_line, global_status, status
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.channels import NotificationChannelRepository
from tests.unit.fake_interactions import FakeFollowup, FakeResponse, FakeUser
from tests.unit.fakes import FakeDatabase

_OWNER_ID = 947795908119109652
_GUILD_ID = 1


class FakeGuildsRepo:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    async def is_enabled(self, guild_id: int) -> bool:
        return self.enabled


class FakeMonitoring:
    poll_intervals = {"youtube": 30.0, "kick": 30.0, "instagram": 48.0}
    registered_platforms: Dict[str, int] = {}

    async def health_snapshot(self) -> Dict[str, Any]:
        return {}


class FakeBot:
    def __init__(self, db: FakeDatabase, *, enabled: bool = True) -> None:
        db.is_connected = True  # type: ignore[attr-defined]  # the real Database exposes this
        self.database = db
        self.guilds_repo = FakeGuildsRepo(enabled)
        self.accounts_repo = PlatformAccountRepository(db)  # type: ignore[arg-type]
        self.channels_repo = NotificationChannelRepository(db)  # type: ignore[arg-type]
        self.alert_configs_repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]
        self.monitoring = FakeMonitoring()
        self.billing = BillingService(BillingRepository(db), enabled=False)  # type: ignore[arg-type]
        self.latency = 0.05
        self.guilds: List[Any] = [object(), object()]
        self.command_prefix = "!"

    async def is_owner(self, user: Any) -> bool:
        return user.id == _OWNER_ID


class FakeGuild:
    id = _GUILD_ID
    name = "Hunter Bots"


class FakeInteraction:
    def __init__(self, bot: FakeBot, *, user_id: int = 123) -> None:
        self.client = bot
        self.guild = FakeGuild()
        self.guild_id = _GUILD_ID
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


def _row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "acc-1",
        "platform": "youtube",
        "platform_account_id": "UCabc",
        "username": "@lifeafterwinning",
        "enabled": True,
        "status": "active",
        "last_checked_at": "2026-10-13T08:00:00+00:00",
        "last_error": None,
    }
    row.update(overrides)
    return row


def _line(row: Dict[str, Any], **kwargs: Any) -> str:
    defaults: Dict[str, Any] = {
        "guild_id": _GUILD_ID,
        "channels": [{"channel_id": 555, "is_valid": True}],
        "configs": [{"enabled": True, "mention_role_id": None}],
        "live_state": None,
        "last_alert_at": None,
    }
    defaults.update(kwargs)
    return format_account_line(row, **defaults)


# ── one account's line ──────────────────────────────────────────────────


def test_account_line_links_the_account_and_names_its_channel() -> None:
    line = _line(_row())

    assert "[@lifeafterwinning](https://www.youtube.com/channel/UCabc)" in line
    assert "→ <#555>" in line
    assert "1/1 alerts on" in line
    assert "checked <t:" in line


def test_account_line_shows_the_mention_with_a_single_at_sign_for_everyone() -> None:
    line = _line(_row(), configs=[{"enabled": True, "mention_role_id": _GUILD_ID}])

    assert "pings @everyone" in line
    assert "<@&1>" not in line


def test_account_line_shows_live_state_and_last_alert() -> None:
    line = _line(
        _row(platform="kick", username="somestreamer", platform_account_id="12345"),
        live_state={"is_live": True, "started_at": "2026-10-13T07:00:00+00:00"},
        last_alert_at="2026-10-13T07:00:30+00:00",
    )

    assert "[somestreamer](https://kick.com/somestreamer)" in line
    assert "🔴 **live** since <t:" in line
    assert "last alert <t:" in line


def test_account_line_flags_problems() -> None:
    line = _line(
        _row(status="invalid", last_error="Instagram account not found"),
        channels=[{"channel_id": 555, "is_valid": False}],
    )

    assert line.startswith("🔴")
    assert "⚠️ <#555>" in line
    assert "⚠️ can't be found any more — remove it and add it again" in line
    assert "Instagram account not found" not in line  # the raw error is for the log, not for members


def test_a_temporary_error_is_one_calm_sentence_with_no_technical_text() -> None:
    line = _line(_row(status="error", last_error="Instagram API error 400: Cannot call API for app 2034048970642813"))

    assert "couldn't be checked just now — retrying automatically" in line
    assert "Instagram API" not in line and "Cannot call API" not in line and "2034048970642813" not in line


def test_account_line_without_a_channel_says_so() -> None:
    assert "no alert channel" in _line(_row(), channels=[])


# ── /notify status ─────────────────────────────────────────────────


async def test_server_status_lists_only_this_servers_accounts(db: FakeDatabase) -> None:
    bot = FakeBot(db)
    mine = await bot.accounts_repo.add(_GUILD_ID, "youtube", "UCabc", "@lifeafterwinning")
    await bot.accounts_repo.add(2, "youtube", "UCother", "@someone-elses")
    channel = await bot.channels_repo.get_or_create(_GUILD_ID, 555, name="alerts")
    await bot.channels_repo.link_account(_GUILD_ID, mine["id"], channel["id"])
    await bot.alert_configs_repo.seed_defaults(_GUILD_ID, mine["id"], ["video_published", "short_published"])
    interaction = FakeInteraction(bot)

    await status.callback(interaction)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "Hunter Bots — DiscoNotify status" in text
    assert "**1** account(s) · **1** alert channel(s)" in text
    assert "@lifeafterwinning" in text and "@someone-elses" not in text
    assert "→ <#555>" in text
    assert "YouTube every 30s" in text


async def test_server_status_with_no_accounts_says_how_to_add_one(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db))

    await status.callback(interaction)  # type: ignore[arg-type]

    assert "/notify account-add" in interaction.response.messages[0]["content"]


async def test_server_status_shows_when_monitoring_is_paused(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db, enabled=False))

    await status.callback(interaction)  # type: ignore[arg-type]

    assert "paused" in interaction.response.messages[0]["content"]


# ── /notify global status ──────────────────────────────────────────


async def test_global_status_is_refused_for_anyone_but_the_bot_owner(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=123)

    await global_status.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    assert "Only the bot owner" in reply["content"]


async def test_global_status_works_for_the_bot_owner_privately(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=_OWNER_ID)

    await global_status.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert reply["ephemeral"] is True
    assert "DiscoNotify Global Status" in reply["content"]
    assert "**2** server(s)" in reply["content"]


# ── /notify help ───────────────────────────────────────────────────


async def test_help_is_grouped_and_lists_owner_tools_for_the_owner(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=_OWNER_ID)

    await help_command.callback(interaction)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    for section in ("Get started", "Alerts", "Accounts", "Server", "Owner only"):
        assert section in text
    assert "`/notify account-add` 🛡️ — Start monitoring" in text
    assert "`/notify global status`" in text
    assert "`!maintenance on|off`" in text and "`!whitelist add|remove|list`" in text


async def test_help_hides_owner_only_commands_from_everyone_else(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=123)

    await help_command.callback(interaction)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "global status" not in text and "Owner only" not in text and "maintenance" not in text
    assert "`/notify status`" in text


async def test_help_puts_subscribe_first_when_billing_is_on(db: FakeDatabase) -> None:
    import discord

    bot = FakeBot(db)
    bot.billing = BillingService(BillingRepository(db), enabled=True)  # type: ignore[arg-type]
    db.tables["notify_guilds"].rows.append({"guild_id": _GUILD_ID, "enabled": True})
    interaction = FakeInteraction(bot)

    await help_command.callback(interaction)  # type: ignore[arg-type]

    view = interaction.response.messages[0]["view"]
    first = [child for child in view.children if isinstance(child, discord.ui.Container)][0]
    first_text = " ".join(item.content for item in first.walk_children() if isinstance(item, discord.ui.TextDisplay))
    assert "Subscription" in first_text and "`/notify subscribe`" in first_text
    assert "Free trial" in first_text  # this server's state


async def test_help_has_no_subscribe_box_while_billing_is_off(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db))

    await help_command.callback(interaction)  # type: ignore[arg-type]

    assert "Subscription" not in interaction.response.messages[0]["content"]


# ── subscription section + card colours ─────────────────────────────────

_DAY = datetime.timedelta(days=1)
_T = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.timezone.utc)


def _details(reason: str, row: Dict[str, Any], *, allowed: bool = True, until: Optional[datetime.datetime] = None) -> str:
    from billing.access import Access, AccessReason, subscription_details

    access = Access(allowed, AccessReason(reason), until)
    return "\n".join(subscription_details(access, row, price_label="$2.99"))


def test_subscription_section_for_a_renewing_card() -> None:
    text = _details("paid", {"card_status": "active"}, until=_T + 20 * _DAY)
    assert "Paid until" in text and "renews automatically" in text
    assert "Cancel any time: `/notify cancel`" in text


def test_subscription_section_for_a_cancelled_card() -> None:
    text = _details("paid", {"card_status": "active", "card_cancels_at": (_T + 20 * _DAY).isoformat()}, until=_T + 20 * _DAY)
    assert "cancelled — no further charges" in text


def test_subscription_section_during_the_trial_with_a_saved_card() -> None:
    text = _details("trial", {"card_status": "trialing"}, until=_T + 3 * _DAY)
    assert "first $2.99 charge when the trial ends" in text


def test_subscription_section_for_crypto() -> None:
    assert "Prepaid with crypto" in _details("paid", {"paid_until": (_T + 80 * _DAY).isoformat()}, until=_T + 80 * _DAY)


def test_subscription_section_when_whitelisted_has_nothing_to_manage() -> None:
    text = _details("whitelisted", {"whitelisted": True})
    assert "Whitelisted" in text and "subscribe" not in text


def test_subscription_section_when_expired_says_how_to_turn_alerts_back_on() -> None:
    text = _details("expired", {}, allowed=False)
    assert "Turn alerts back on: `/notify subscribe` — $2.99/month" in text


async def test_status_shows_the_subscription_section_and_turns_red_without_access(db: FakeDatabase) -> None:
    bot = FakeBot(db)
    bot.billing = BillingService(BillingRepository(db), enabled=True)  # type: ignore[arg-type]
    db.tables["notify_guilds"].rows.append({"guild_id": _GUILD_ID, "enabled": True})
    db.tables["notify_billing"].rows.append({"guild_id": _GUILD_ID, "trial_ends_at": "2020-01-01T00:00:00+00:00"})
    interaction = FakeInteraction(bot)

    await status.callback(interaction)  # type: ignore[arg-type]

    reply = interaction.response.messages[0]
    assert "**💳 Subscription**" in reply["content"]
    assert "Turn alerts back on" in reply["content"]
    from config import constants

    container = reply["view"].children[0]
    assert container.accent_colour == constants.COLOR_LOGO_RED


def test_normal_cards_are_white_and_errors_are_logo_red() -> None:
    from config import constants
    from ui import cards as ui_cards

    assert ui_cards.card("ok").children[0].accent_colour == 0xFFFFFF
    assert ui_cards.card("done", tone=ui_cards.SUCCESS).children[0].accent_colour == 0xFFFFFF
    assert ui_cards.card("failed", tone=ui_cards.ERROR).children[0].accent_colour == constants.COLOR_LOGO_RED == 0xE2364A
    assert ui_cards.card("careful", tone=ui_cards.WARNING).children[0].accent_colour == 0xE2364A


async def test_status_with_many_long_accounts_stays_under_discords_limit(db: FakeDatabase) -> None:
    """25 accounts with long names and error notes came to ~7,500 characters
    — Discord rejects anything over 4,000, so the command failed."""
    import discord

    bot = FakeBot(db)
    for i, platform in enumerate(["youtube"] * 10 + ["kick"] * 5 + ["instagram"] * 5 + ["twitter"] * 5):
        row = await bot.accounts_repo.add(_GUILD_ID, platform, f"id{i}" * 8, f"@a_very_long_creator_name_{i:02d}")
        db.tables["notify_platform_accounts"].rows[-1].update(status="error", last_error="something went wrong " * 10)
        channel = await bot.channels_repo.get_or_create(_GUILD_ID, 10**17 + i, name=f"alerts-{i}")
        await bot.channels_repo.link_account(_GUILD_ID, row["id"], channel["id"])
    interaction = FakeInteraction(bot)

    await status.callback(interaction)  # type: ignore[arg-type]

    view = interaction.response.messages[0]["view"]
    total = sum(len(item.content) for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
    assert total <= 4000
    assert "more — `/notify account-list`" in interaction.response.messages[0]["content"]
