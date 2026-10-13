"""Where billing meets the rest of the bot: the monitoring scheduler only
polls servers that may be monitored (fixes /pulsenotify toggle off never
actually pausing anything), the command gate, and the billing commands."""

from __future__ import annotations

from typing import Any, List, Set

import pytest

from billing.access import Access, AccessReason
from bot.client import PulseNotifyCommandTree
from commands.admin.billing import subscribe
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

    async def access(self, guild_id: int) -> Access:
        return Access(self._allowed, AccessReason.TRIAL if self._allowed else AccessReason.EXPIRED)

    async def billing_row(self, guild_id: int) -> Any:
        return None

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


class FakeBot:
    def __init__(self, *, allowed: bool = True, enabled: bool = True) -> None:
        self.guilds_repo = FakeGuildsRepo(enabled)
        self.billing = FakeBilling(allowed)

    async def is_owner(self, user: Any) -> bool:
        return user.id == _OWNER

    def get_guild(self, guild_id: int) -> Any:
        return None


class FakeGuild:
    id = 1


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
    allowed = await PulseNotifyCommandTree.interaction_check(FakeTree(bot), interaction)  # type: ignore[arg-type]
    return allowed, interaction


async def test_a_server_without_access_is_told_to_subscribe() -> None:
    allowed, interaction = await _check(FakeBot(allowed=False), "pulsenotify account-add")

    assert allowed is False
    assert "/pulsenotify subscribe" in interaction.response.messages[0]["content"]


@pytest.mark.parametrize(
    "command", ["pulsenotify subscribe", "pulsenotify status", "pulsenotify help", "pulsenotify toggle", "pulsenotify global status"]
)
async def test_billing_and_owner_commands_always_work(command: str) -> None:
    allowed, _ = await _check(FakeBot(allowed=False, enabled=False), command)

    assert allowed is True


async def test_a_server_with_access_can_use_everything() -> None:
    allowed, _ = await _check(FakeBot(allowed=True), "pulsenotify account-add")

    assert allowed is True


# ── billing commands ────────────────────────────────────────────────────


async def test_subscribe_says_free_while_billing_is_off() -> None:
    bot = FakeBot()
    bot.billing.enabled = False
    interaction = FakeInteraction(bot)

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    assert "free" in interaction.response.messages[0]["content"].lower()


async def test_subscribe_shows_card_and_crypto_buttons() -> None:
    interaction = FakeInteraction(FakeBot())

    await subscribe.callback(interaction)  # type: ignore[arg-type]

    import discord

    view = interaction.response.messages[0]["view"]
    labels = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert any("Card" in label for label in labels) and any("Crypto" in label for label in labels)
    assert "never charged" in interaction.response.messages[0]["content"]


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
    from commands.pulsenotify_group import pulsenotify_group
    from discord import app_commands

    import commands.admin.billing  # noqa: F401  (registers /pulsenotify subscribe)

    names = [c.qualified_name for c in pulsenotify_group.walk_commands() if isinstance(c, app_commands.Command)]
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


async def test_a_paddle_rejection_is_shown_by_name_not_as_a_generic_error() -> None:
    """Real case 2026-10-13: a PaddleError showed only "Something went wrong"."""
    from billing.paddle import PaddleError
    from commands.admin.billing import _report_error

    interaction = FakeInteraction(FakeBot())
    await _report_error(interaction, PaddleError("Paddle rejected the API key (403): not authorized to read price"))  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "not authorized to read price" in text
    assert "Something went wrong" not in text
