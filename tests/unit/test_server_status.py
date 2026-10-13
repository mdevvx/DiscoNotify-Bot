"""/pulsenotify status (this server only), /pulsenotify global status
(bot owners only), and /pulsenotify help listing commands nested in the
"global" subcommand group."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pytest

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
    assert "⚠️ Instagram account not found" in line


def test_account_line_without_a_channel_says_so() -> None:
    assert "no alert channel" in _line(_row(), channels=[])


# ── /pulsenotify status ─────────────────────────────────────────────────


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
    assert "Hunter Bots — PulseNotify status" in text
    assert "**1** account(s) · **1** alert channel(s)" in text
    assert "@lifeafterwinning" in text and "@someone-elses" not in text
    assert "→ <#555>" in text
    assert "YouTube every 30s" in text


async def test_server_status_with_no_accounts_says_how_to_add_one(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db))

    await status.callback(interaction)  # type: ignore[arg-type]

    assert "/pulsenotify account-add" in interaction.response.messages[0]["content"]


async def test_server_status_shows_when_monitoring_is_paused(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db, enabled=False))

    await status.callback(interaction)  # type: ignore[arg-type]

    assert "paused" in interaction.response.messages[0]["content"]


# ── /pulsenotify global status ──────────────────────────────────────────


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
    assert "PulseNotify Global Status" in reply["content"]
    assert "**2** server(s)" in reply["content"]


# ── /pulsenotify help ───────────────────────────────────────────────────


async def test_help_lists_nested_commands_by_full_name_for_the_owner(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=_OWNER_ID)

    await help_command.callback(interaction)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "**/pulsenotify global status**" in text
    assert "**/pulsenotify status**" in text


async def test_help_hides_owner_only_commands_from_everyone_else(db: FakeDatabase) -> None:
    interaction = FakeInteraction(FakeBot(db), user_id=123)

    await help_command.callback(interaction)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "global status" not in text
    assert "**/pulsenotify status**" in text
