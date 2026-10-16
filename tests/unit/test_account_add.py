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
        self.calls = 0

    async def validate_account(self, identifier: str) -> AccountRef:
        self.calls += 1
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

    text = interaction.followup.messages[0]["content"]
    assert "We couldn't find that YouTube account" in text and "(Reference `" in text
    assert "no such channel" not in text  # the detail goes to the log, not to members
    assert await interaction.client.accounts_repo.list_by_guild(1) == []


async def test_an_error_that_carries_a_member_message_shows_exactly_that(db: FakeDatabase) -> None:
    error = PermanentPlatformError("YouTube API 404: x", user_message="We couldn't find that YouTube channel. Please check the @handle.")
    interaction = await _submit(db, adapter=FakeAdapter(error=error))

    assert interaction.followup.messages[0]["content"].count("We couldn't find that YouTube channel. Please check the @handle.") == 1
    assert "404" not in interaction.followup.messages[0]["content"]


async def test_a_busy_platform_says_when_to_try_again_and_that_nothing_was_added(db: FakeDatabase) -> None:
    from utils.errors import RateLimitedError

    interaction = await _submit(db, adapter=FakeAdapter(error=RateLimitedError("Instagram API rate limited: code 4", retry_after=130)))

    text = interaction.followup.messages[0]["content"]
    assert "busy right now" in text and "about 3 minutes" in text and "Nothing was added" in text
    assert "code 4" not in text


async def test_adding_the_same_account_twice_says_so_and_where_its_alerts_go(db: FakeDatabase) -> None:
    await _submit(db)
    adapter = FakeAdapter()
    interaction = await _submit(db, adapter=adapter)

    text = (interaction.response.messages or interaction.followup.messages)[0]["content"]
    assert "You've already added **@example** (YouTube) in this server." in text
    assert "Its alerts go to <#555>." in text and "/pulsenotify account-list" in text
    assert adapter.calls == 0  # caught before asking YouTube anything
    assert len(db.tables["pulsenotify_platform_accounts"].rows) == 1


@pytest.mark.parametrize("spelling", ["@example", "@EXAMPLE", "  @Example  ", "https://www.youtube.com/@example", "youtube.com/@Example/videos", "https://youtube.com/channel/UCabc"])
async def test_every_spelling_of_the_same_youtube_account_is_recognised_without_a_lookup(db: FakeDatabase, spelling: str) -> None:
    await _submit(db)
    adapter = FakeAdapter()

    interaction = await _submit(db, adapter=adapter, identifier=spelling)

    assert "You've already added **@example**" in (interaction.response.messages or interaction.followup.messages)[0]["content"]
    assert adapter.calls == 0


async def test_a_spelling_only_the_platform_can_resolve_is_still_caught_after_the_lookup(db: FakeDatabase) -> None:
    """A /c/Name link could be any channel until YouTube says which."""
    await _submit(db)
    adapter = FakeAdapter()

    interaction = await _submit(db, adapter=adapter, identifier="https://www.youtube.com/c/SomeOldName")

    assert "You've already added **@example**" in interaction.followup.messages[0]["content"]
    assert adapter.calls == 1
    assert len(db.tables["pulsenotify_platform_accounts"].rows) == 1


async def test_a_different_account_with_a_similar_name_is_not_a_duplicate(db: FakeDatabase) -> None:
    class OtherChannel(FakeAdapter):
        async def validate_account(self, identifier: str) -> AccountRef:
            return AccountRef(platform="youtube", platform_account_id="UCother", username="@example_two")

    await _submit(db)

    interaction = await _submit(db, adapter=OtherChannel(), identifier="@example_two")

    assert "already added" not in str(interaction.followup.messages + interaction.response.messages)
    assert [row["username"] for row in db.tables["pulsenotify_platform_accounts"].rows] == ["@example", "@example_two"]


@pytest.mark.parametrize(
    ("platform", "stored_username", "stored_id", "spellings"),
    [
        ("youtube", "@nasa", "UCLA_DiR1FfKNvjuUpBHmylQ", ["@NASA", "https://www.youtube.com/@nasa", "youtube.com/channel/UCLA_DiR1FfKNvjuUpBHmylQ", "UCLA_DiR1FfKNvjuUpBHmylQ"]),
        ("kick", "xqc", "676", ["xqc", "XQC", "@xqc", "https://kick.com/xqc", "kick.com/XQC/"]),
        ("instagram", "pajamabillionaire", "17841400572798814", ["pajamabillionaire", "@PajamaBillionaire", "https://www.instagram.com/pajamabillionaire/", "instagram.com/pajamabillionaire?igsh=abc"]),
        ("twitter", "Cookedcom", "2008104784159805440", ["@cookedcom", "Cookedcom", "https://x.com/cookedcom", "https://twitter.com/Cookedcom/status/2110162914594443689", "x.com/cookedcom/"]),
    ],
)
def test_duplicates_are_recognised_on_every_platform(platform: str, stored_username: str, stored_id: str, spellings: list) -> None:
    from commands.accounts.modals import find_duplicate

    rows = [{"id": "row-1", "platform": platform, "username": stored_username, "platform_account_id": stored_id}]

    for spelling in spellings:
        assert find_duplicate(rows, platform, spelling) is rows[0], spelling


def test_the_same_name_on_another_platform_or_another_account_is_not_a_duplicate() -> None:
    from commands.accounts.modals import find_duplicate

    rows = [{"id": "row-1", "platform": "instagram", "username": "nasa", "platform_account_id": "1784"}]

    assert find_duplicate(rows, "twitter", "@nasa") is None  # a different platform
    assert find_duplicate(rows, "instagram", "@nasa_hq") is None  # a different account
    assert find_duplicate(rows, "instagram", "https://www.instagram.com/p/AbC123/") is None  # a post link
    assert find_duplicate(rows, "instagram", "   ") is None


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


async def test_a_token_or_permission_problem_is_never_shown_to_members(db: FakeDatabase) -> None:
    """Meta refusing the bot's own token is the bot owner's to fix (the log has
    it, under the reference code) — members only hear that it isn't responding."""
    from utils.errors import TransientPlatformError

    error = TransientPlatformError("Instagram access token is missing a permission: Cannot call API for app 2034048970642813")
    interaction = await _submit(db, adapter=FakeAdapter(error=error))

    message = interaction.followup.messages[0]["content"]
    assert "isn't responding right now" in message and "(Reference `" in message
    for technical in ("token", "permission", "Cannot call API", "2034048970642813"):
        assert technical not in message
    assert await interaction.client.accounts_repo.list_by_guild(1) == []
