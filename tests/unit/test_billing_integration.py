"""Where billing meets the rest of the bot: the monitoring scheduler only
polls servers that may be monitored (fixes /pulsenotify toggle off never
actually pausing anything), the command gate, and the billing commands."""

from __future__ import annotations

from typing import Any, List, Set

import pytest

from billing.access import Access, AccessReason
from bot.client import PulseNotifyCommandTree
from commands.admin.billing import subscribe, whitelist_add, whitelist_list, whitelist_remove
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
    "command", ["pulsenotify subscribe", "pulsenotify status", "pulsenotify help", "pulsenotify toggle", "pulsenotify global whitelist-add"]
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


@pytest.mark.parametrize("command", [whitelist_add, whitelist_remove, whitelist_list])
async def test_whitelist_commands_refuse_anyone_but_the_owner(command: Any) -> None:
    bot = FakeBot()
    interaction = FakeInteraction(bot, user_id=5)

    await command.callback(interaction)  # type: ignore[arg-type]

    assert "Only the bot owner" in interaction.response.messages[0]["content"]
    assert bot.billing.whitelisted == []


async def test_owner_can_whitelist_a_server_by_id_and_list_it() -> None:
    bot = FakeBot()

    await whitelist_add.callback(FakeInteraction(bot, user_id=_OWNER), "1466009969806545009", "friend")  # type: ignore[arg-type]
    listing = FakeInteraction(bot, user_id=_OWNER)
    await whitelist_list.callback(listing)  # type: ignore[arg-type]

    assert bot.billing.whitelisted == [1466009969806545009]
    assert "1466009969806545009" in listing.response.messages[0]["content"]


async def test_whitelist_rejects_something_that_is_not_a_server_id() -> None:
    bot = FakeBot()
    interaction = FakeInteraction(bot, user_id=_OWNER)

    await whitelist_add.callback(interaction, "my server", None)  # type: ignore[arg-type]

    assert "not a server ID" in interaction.response.messages[0]["content"]
    assert bot.billing.whitelisted == []
