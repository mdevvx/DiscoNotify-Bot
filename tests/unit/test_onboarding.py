"""Joining a new server: commands synced there (unless already global),
trial started, and an intro card posted in a channel the bot can use."""

from __future__ import annotations

import datetime
from typing import Any, List, Optional

from billing.access import Access, AccessReason
from bot import onboarding
from ui.cards import plain_text


class FakePermissions:
    def __init__(self, allowed: bool) -> None:
        self.view_channel = allowed
        self.send_messages = allowed


class FakeChannel:
    def __init__(self, name: str, position: int, *, can_post: bool = True) -> None:
        self.name = name
        self.position = position
        self._can_post = can_post
        self.sent: List[Any] = []

    def permissions_for(self, member: Any) -> FakePermissions:
        return FakePermissions(self._can_post)

    async def send(self, *, view: Any = None, allowed_mentions: Any = None) -> None:
        self.sent.append(view)


class FakeGuild:
    def __init__(self, channels: List[FakeChannel], system_channel: Optional[FakeChannel] = None) -> None:
        self.id = 42
        self.me = object()
        self.text_channels = channels
        self.system_channel = system_channel


class FakeCommand:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeTree:
    def __init__(self, global_names: List[str], *, fail: bool = False) -> None:
        self._global = [FakeCommand(name) for name in global_names]
        self._fail = fail
        self.copied_to: List[Any] = []
        self.synced_to: List[Any] = []

    async def fetch_commands(self):
        if self._fail:
            raise RuntimeError("discord down")
        return self._global

    def copy_global_to(self, *, guild: Any) -> None:
        self.copied_to.append(guild)

    async def sync(self, *, guild: Any = None):
        self.synced_to.append(guild)
        return [FakeCommand("pulsenotify")]


class FakeBilling:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.price_label = "$2.99"
        self.started: List[int] = []

    async def access(self, guild_id: int) -> Access:
        self.started.append(guild_id)
        return Access(True, AccessReason.TRIAL, datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc))


class FakeSettings:
    billing_trial_days = 7


class FakeBot:
    def __init__(self, *, global_names: List[str] = (), billing: bool = False, sync_fails: bool = False) -> None:
        self.tree = FakeTree(list(global_names), fail=sync_fails)
        self.billing = FakeBilling(billing)
        self.settings = FakeSettings()


# ── command sync ────────────────────────────────────────────────────────


async def test_commands_are_synced_to_a_new_server_when_not_global() -> None:
    bot = FakeBot()
    guild = FakeGuild([FakeChannel("general", 0)])

    result = await onboarding.sync_commands_to(bot, guild)

    assert result == "synced"
    assert bot.tree.copied_to == [guild] and bot.tree.synced_to == [guild]


async def test_already_global_commands_are_not_copied_again() -> None:
    """Copying global commands into a server shows each one twice there."""
    bot = FakeBot(global_names=["pulsenotify"])

    result = await onboarding.sync_commands_to(bot, FakeGuild([]))

    assert result == "global"
    assert bot.tree.copied_to == [] and bot.tree.synced_to == []


# ── intro channel ───────────────────────────────────────────────────────


def test_intro_goes_to_the_system_channel_when_the_bot_can_post_there() -> None:
    welcome = FakeChannel("welcome", 5)
    guild = FakeGuild([FakeChannel("general", 0), welcome], system_channel=welcome)

    assert onboarding.pick_intro_channel(guild) is welcome


def test_intro_falls_back_to_the_first_channel_the_bot_can_post_in() -> None:
    locked = FakeChannel("rules", 0, can_post=False)
    general = FakeChannel("general", 1)
    guild = FakeGuild([general, locked], system_channel=FakeChannel("welcome", 9, can_post=False))

    assert onboarding.pick_intro_channel(guild) is general


def test_no_intro_channel_when_the_bot_cannot_post_anywhere() -> None:
    assert onboarding.pick_intro_channel(FakeGuild([FakeChannel("rules", 0, can_post=False)])) is None


# ── intro card ──────────────────────────────────────────────────────────


def test_intro_lists_the_setup_commands() -> None:
    text = plain_text(onboarding.intro_card(FakeBot(), None))

    assert "Thanks for adding PulseNotify" in text
    for command in ("/pulsenotify account-add", "/pulsenotify alerts", "/pulsenotify status", "/pulsenotify help"):
        assert command in text
    assert "Free trial" not in text  # billing off


def test_intro_mentions_the_trial_and_price_when_billing_is_on() -> None:
    access = Access(True, AccessReason.TRIAL, datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc))

    text = plain_text(onboarding.intro_card(FakeBot(billing=True), access))

    assert "7-day free trial has started" in text
    assert "/pulsenotify subscribe" in text and "$2.99/month" in text


# ── the whole join ──────────────────────────────────────────────────────


async def test_joining_syncs_starts_the_trial_and_posts_the_intro() -> None:
    bot = FakeBot(billing=True)
    general = FakeChannel("general", 0)
    guild = FakeGuild([general])

    await onboarding.on_join(bot, guild)  # type: ignore[arg-type]

    assert bot.tree.synced_to == [guild]
    assert bot.billing.started == [42]
    assert "Free trial" in plain_text(general.sent[0])


async def test_a_failed_sync_does_not_stop_the_intro() -> None:
    bot = FakeBot(sync_fails=True)
    general = FakeChannel("general", 0)

    await onboarding.on_join(bot, FakeGuild([general]))  # type: ignore[arg-type]

    assert len(general.sent) == 1
