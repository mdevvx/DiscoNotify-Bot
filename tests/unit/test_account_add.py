"""`/pulsenotify account-add`: the command opens a per-platform form, and
the form's apply() validates the account, links the channel, seeds the
default alerts and (optionally) the mention role."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from commands.accounts.account_commands import account_add
from commands.accounts.modals import AddAccountModal
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.channels import NotificationChannelRepository
from platforms.base import AccountRef, Capability
from tests.unit.fake_interactions import FakeFollowup, FakeResponse, FakeUser
from tests.unit.fakes import FakeDatabase
from utils.errors import PermanentPlatformError


class FakeChoice:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class FakePermissions:
    def __init__(self, allowed: bool) -> None:
        self.view_channel = allowed
        self.send_messages = allowed


class FakeTextChannel:
    def __init__(self, channel_id: int = 555, *, bot_can_post: bool = True) -> None:
        self.id = channel_id
        self.name = "alerts"
        self.mention = f"<#{channel_id}>"
        self._bot_can_post = bot_can_post

    def permissions_for(self, member: Any) -> FakePermissions:
        return FakePermissions(self._bot_can_post)


class FakeAdapter:
    capabilities = frozenset({Capability.VIDEOS, Capability.SHORTS, Capability.LIVE_STATUS})

    def __init__(self, *, error: Optional[Exception] = None) -> None:
        self.error = error

    async def validate_account(self, identifier: str) -> AccountRef:
        if self.error is not None:
            raise self.error
        return AccountRef(platform="youtube", platform_account_id="UCabc", username="@example")


class FakeMonitoring:
    def __init__(self, adapter: Optional[FakeAdapter]) -> None:
        self._adapter = adapter

    def get_adapter(self, platform: str) -> Optional[FakeAdapter]:
        return self._adapter


class FakeSender:
    def __init__(self) -> None:
        self.webhook_requests: List[int] = []

    async def get_or_create_webhook(self, channel_row: Dict[str, Any], channel: Any) -> None:
        self.webhook_requests.append(channel.id)


class FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.me = object()
        self.channels = {555: FakeTextChannel()}

    def get_channel(self, channel_id: int) -> Any:
        return self.channels.get(channel_id)


class FakeClient:
    def __init__(self, db: FakeDatabase, adapter: Optional[FakeAdapter]) -> None:
        self.accounts_repo = PlatformAccountRepository(db)  # type: ignore[arg-type]
        self.alert_configs_repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]
        self.channels_repo = NotificationChannelRepository(db)  # type: ignore[arg-type]
        self.monitoring = FakeMonitoring(adapter)
        self.notification_sender = FakeSender()
        self.settings = type("S", (), {"max_accounts_per_server": 25})()


class FakeInteraction:
    def __init__(self, db: FakeDatabase, adapter: Optional[FakeAdapter] = None) -> None:
        self.guild = FakeGuild()
        self.user = FakeUser(100)
        self.guild_id = self.guild.id
        self.client = FakeClient(db, adapter)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


async def _submit(
    db: FakeDatabase,
    *,
    adapter: Optional[FakeAdapter] = None,
    channel: Any = None,
    role_id: Optional[int] = None,
    identifier: str = "@example",
) -> FakeInteraction:
    interaction = FakeInteraction(db, adapter or FakeAdapter())
    modal = AddAccountModal("youtube")
    await modal.apply(  # type: ignore[arg-type]
        interaction, identifier=identifier, channel=channel or FakeTextChannel(), role_id=role_id
    )
    return interaction


@pytest.mark.parametrize("platform", ["youtube", "kick", "instagram"])
async def test_account_add_opens_a_form_for_the_chosen_platform(platform: str, db: FakeDatabase) -> None:
    interaction = FakeInteraction(db)

    await account_add.callback(interaction, FakeChoice(platform.title(), platform))  # type: ignore[arg-type]

    modal = interaction.response.modals[0]
    assert isinstance(modal, AddAccountModal)
    assert modal.platform == platform


async def test_submitting_the_form_starts_monitoring_with_default_alerts(db: FakeDatabase) -> None:
    interaction = await _submit(db)

    accounts = await interaction.client.accounts_repo.list_by_guild(1)
    assert [row["username"] for row in accounts] == ["@example"]
    configs = await interaction.client.alert_configs_repo.list_for_account(accounts[0]["id"])
    assert {config["event_type"] for config in configs} >= {"video_published", "stream_started"}
    assert interaction.client.notification_sender.webhook_requests == [555]
    assert "Now monitoring **@example**" in interaction.followup.messages[0]["content"]


async def test_a_role_picked_in_the_form_is_mentioned_on_every_alert_type(db: FakeDatabase) -> None:
    interaction = await _submit(db, role_id=777)

    account_id = (await interaction.client.accounts_repo.list_by_guild(1))[0]["id"]
    configs = await interaction.client.alert_configs_repo.list_for_account(account_id)
    assert {config["mention_role_id"] for config in configs} == {777}
    assert "<@&777>" in interaction.followup.messages[0]["content"]


async def test_everyone_picked_in_the_form_confirms_with_a_single_at_sign(db: FakeDatabase) -> None:
    interaction = await _submit(db, role_id=1)  # the @everyone role's ID is the guild's ID

    assert "@everyone" in interaction.followup.messages[0]["content"]
    assert "<@&1>" not in interaction.followup.messages[0]["content"]


async def test_a_channel_the_bot_cannot_post_in_is_refused_up_front(db: FakeDatabase) -> None:
    interaction = await _submit(db, channel=FakeTextChannel(bot_can_post=False))

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "Send Messages" in interaction.response.messages[0]["content"]
    assert await interaction.client.accounts_repo.list_by_guild(1) == []


async def test_an_unknown_account_is_reported_and_nothing_is_saved(db: FakeDatabase) -> None:
    interaction = await _submit(db, adapter=FakeAdapter(error=PermanentPlatformError("no such channel")))

    assert "Couldn't find that YouTube account" in interaction.followup.messages[0]["content"]
    assert await interaction.client.accounts_repo.list_by_guild(1) == []


async def test_adding_the_same_account_twice_says_so(db: FakeDatabase) -> None:
    await _submit(db)
    interaction = await _submit(db)

    assert "already being monitored" in interaction.followup.messages[0]["content"]


async def test_a_server_at_its_account_limit_cant_add_more(db: FakeDatabase) -> None:
    """Shared API limits (YouTube quota, Instagram calls, X window) must not
    be used up by one server."""
    for n in range(25):
        db.tables["pulsenotify_platform_accounts"].rows.append(
            {"id": f"acc-{n}", "guild_id": 1, "platform": "youtube", "platform_account_id": f"UC{n}", "username": f"c{n}"}
        )

    interaction = await _submit(db)

    assert "already monitors 25 accounts" in interaction.response.messages[0]["content"]
    assert len(db.tables["pulsenotify_platform_accounts"].rows) == 25


async def test_a_blank_identifier_is_refused(db: FakeDatabase) -> None:
    interaction = await _submit(db, identifier="   ")

    assert interaction.response.messages[0]["ephemeral"] is True
    assert interaction.response.deferred is False


# ── submitting the real form ────────────────────────────────────────────


class _Picked:
    def __init__(self, item_id: int) -> None:
        self.id = item_id


def _fill(modal: AddAccountModal, *, identifier: str, channel: Any, role: Any = None) -> None:
    modal.identifier._value = identifier  # what Discord's submit fills in
    modal.channel._values = [channel]
    modal.role._values = [role] if role is not None else []


async def test_submitting_the_form_with_picked_objects_adds_the_account(db: FakeDatabase) -> None:
    interaction = FakeInteraction(db, FakeAdapter())
    modal = AddAccountModal("instagram")
    _fill(modal, identifier="cookedcom", channel=_Picked(555), role=_Picked(777))

    await modal.on_submit(interaction)  # type: ignore[arg-type]

    assert "Now monitoring" in interaction.followup.messages[0]["content"]


async def test_submitting_the_form_with_raw_id_strings_still_works(db: FakeDatabase) -> None:
    """Regression test: discord.py hands back the picked channel/role as a
    raw ID string when it can't build an object from Discord's resolved
    data — `picked.id` on that string crashed the submit, which Discord
    showed as "Something went wrong. Try again." (reported 2026-10-12)."""
    interaction = FakeInteraction(db, FakeAdapter())
    modal = AddAccountModal("instagram")
    _fill(modal, identifier="cookedcom", channel="555", role="777")

    await modal.on_submit(interaction)  # type: ignore[arg-type]

    assert "Now monitoring" in interaction.followup.messages[0]["content"]
    account_id = (await interaction.client.accounts_repo.list_by_guild(1))[0]["id"]
    configs = await interaction.client.alert_configs_repo.list_for_account(account_id)
    assert {config["mention_role_id"] for config in configs} == {777}


async def test_a_form_error_shows_an_error_card_instead_of_discords_generic_message(db: FakeDatabase) -> None:
    interaction = FakeInteraction(db)

    await AddAccountModal("instagram").on_error(interaction, RuntimeError("boom"))  # type: ignore[arg-type]

    assert interaction.response.messages[0]["ephemeral"] is True
    assert "logged" in interaction.response.messages[0]["content"]


def test_form_title_uses_the_right_article() -> None:
    assert AddAccountModal("instagram").title == "Add an Instagram account"
    assert AddAccountModal("youtube").title == "Add a YouTube account"


async def test_a_token_or_permission_problem_is_named_not_hidden(db: FakeDatabase) -> None:
    """Regression test: Meta refusing the bot's own token showed only
    "Couldn't reach Instagram right now", with nothing to act on."""
    from utils.errors import TransientPlatformError

    error = TransientPlatformError("Instagram access token is missing a permission: Cannot call API for app")
    interaction = await _submit(db, adapter=FakeAdapter(error=error))

    message = interaction.followup.messages[0]["content"]
    assert "missing a permission" in message
    assert await interaction.client.accounts_repo.list_by_guild(1) == []
