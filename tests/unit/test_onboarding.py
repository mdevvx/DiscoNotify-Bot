"""Joining a new server: commands synced there (unless already global), and
whoever added the bot DMed the link to activate the free trial, with nothing
posted in the server."""

from __future__ import annotations

from typing import Any, List, Optional

from billing.access import Access, AccessReason
from bot import onboarding


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


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.bot = False


class FakeEntry:
    def __init__(self, user: Any, target_id: int) -> None:
        self.user = user
        self.target = type("T", (), {"id": target_id})()


class FakeMe:
    id = 777

    def __init__(self, can_read_audit_log: bool) -> None:
        self.guild_permissions = type("P", (), {"view_audit_log": can_read_audit_log})()


class FakeGuild:
    def __init__(
        self,
        channels: List[FakeChannel],
        system_channel: Optional[FakeChannel] = None,
        *,
        inviter: Any = None,
        can_read_audit_log: bool = True,
    ) -> None:
        self.id = 42
        self.me = FakeMe(can_read_audit_log)
        self.text_channels = channels
        self.system_channel = system_channel
        self._inviter = inviter

    async def audit_logs(self, *, limit: int, action: Any):
        if self._inviter is not None:
            yield FakeEntry(FakeUser(555), target_id=999)  # someone else's bot
            yield FakeEntry(self._inviter, target_id=self.me.id)


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
        return [FakeCommand("notify")]


class FakeBilling:
    def __init__(self, enabled: bool, reason: AccessReason = AccessReason.NEEDS_ACTIVATION) -> None:
        self.enabled = enabled
        self.reason = reason
        self.started: List[int] = []
        self.invited: List[Any] = []

    async def access(self, guild_id: int) -> Access:
        self.started.append(guild_id)
        return Access(self.reason != AccessReason.NEEDS_ACTIVATION, self.reason)

    async def set_invited_by(self, guild_id: int, user_id: int) -> None:
        self.invited.append((guild_id, user_id))


class FakeGuildsRepo:
    async def is_enabled(self, guild_id: int) -> bool:
        return True

    async def set_present(self, guild_id: int, present: bool) -> None:
        pass


class FakeBot:
    def __init__(self, *, global_names: List[str] = (), billing: bool = False, sync_fails: bool = False,
                 reason: AccessReason = AccessReason.NEEDS_ACTIVATION) -> None:
        self.tree = FakeTree(list(global_names), fail=sync_fails)
        self.billing = FakeBilling(billing, reason)
        self.guilds_repo = FakeGuildsRepo()
        self.dms: List[Any] = []

    async def send_activation_dm(self, guild: Any, user_id: int, *, followup: bool = False) -> bool:
        self.dms.append((guild.id, user_id))
        return True


# ── command sync ────────────────────────────────────────────────────────


async def test_commands_are_synced_to_a_new_server_when_not_global() -> None:
    bot = FakeBot()
    guild = FakeGuild([FakeChannel("general", 0)])

    result = await onboarding.sync_commands_to(bot, guild)

    assert result == "synced"
    assert bot.tree.copied_to == [guild] and bot.tree.synced_to == [guild]


async def test_already_global_commands_are_not_copied_again() -> None:
    """Copying global commands into a server shows each one twice there."""
    bot = FakeBot(global_names=["notify"])

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


# ── the whole join ──────────────────────────────────────────────────────


async def test_joining_syncs_and_dms_the_inviter_the_activation_link_without_posting_anything() -> None:
    bot = FakeBot(billing=True)
    general = FakeChannel("general", 0)
    guild = FakeGuild([general], inviter=FakeUser(9))

    await onboarding.on_join(bot, guild)  # type: ignore[arg-type]

    assert bot.tree.synced_to == [guild]
    assert bot.billing.invited == [(42, 9)]
    assert bot.dms == [(42, 9)]
    assert general.sent == []


async def test_nobody_is_dmed_when_the_inviter_cannot_be_found() -> None:
    bot = FakeBot(billing=True)
    guild = FakeGuild([FakeChannel("general", 0)], inviter=None)

    await onboarding.on_join(bot, guild)  # type: ignore[arg-type]

    assert bot.dms == [] and bot.billing.invited == []


async def test_nobody_is_dmed_without_the_view_audit_log_permission() -> None:
    bot = FakeBot(billing=True)
    guild = FakeGuild([FakeChannel("general", 0)], inviter=FakeUser(9), can_read_audit_log=False)

    await onboarding.on_join(bot, guild)  # type: ignore[arg-type]

    assert bot.dms == []


async def test_a_server_that_already_has_a_trial_is_not_dmed_again() -> None:
    bot = FakeBot(billing=True, reason=AccessReason.TRIAL)

    await onboarding.on_join(bot, FakeGuild([], inviter=FakeUser(9)))  # type: ignore[arg-type]

    assert bot.dms == []


async def test_nothing_is_dmed_when_billing_is_off() -> None:
    bot = FakeBot(billing=False)

    await onboarding.on_join(bot, FakeGuild([], inviter=FakeUser(9)))  # type: ignore[arg-type]

    assert bot.dms == []


async def test_a_failed_sync_does_not_stop_the_activation_dm() -> None:
    bot = FakeBot(billing=True, sync_fails=True)

    await onboarding.on_join(bot, FakeGuild([FakeChannel("general", 0)], inviter=FakeUser(9)))  # type: ignore[arg-type]

    assert bot.dms == [(42, 9)]
