"""Phase 8's per-account alert configuration commands:
account-set-mention (role mentions), alerts (per-event-type enabled/embed
toggles, and viewing them), and account-info (full config, including
seeing a previously-set custom message — there was no way to do that
before this phase)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from commands.accounts.account_commands import account_info, account_set_mention, alerts
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from platforms.base import event_types_for_capabilities, Capability
from tests.unit.fakes import FakeDatabase


class FakeResponse:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def send_message(
        self, content: Optional[str] = None, *, embed=None, ephemeral: bool = False, allowed_mentions=None
    ) -> None:
        self.messages.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral, "allowed_mentions": allowed_mentions}
        )


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class FakeClient:
    def __init__(self, db: FakeDatabase) -> None:
        self.accounts_repo = PlatformAccountRepository(db)  # type: ignore[arg-type]
        self.alert_configs_repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]


class FakeInteraction:
    def __init__(self, guild_id: int, db: FakeDatabase) -> None:
        self.guild = FakeGuild(guild_id)
        self.client = FakeClient(db)
        self.response = FakeResponse()


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class FakeChoice:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


async def _seed_account_with_alerts(interaction: FakeInteraction, guild_id: int = 1) -> str:
    row = await interaction.client.accounts_repo.add(guild_id, "youtube", "UCabc", "SomeCreator")
    assert row is not None
    event_types = event_types_for_capabilities(frozenset({Capability.VIDEOS, Capability.SHORTS}))
    await interaction.client.alert_configs_repo.seed_defaults(guild_id, row["id"], [t.value for t in event_types])
    return row["id"]


# ── account-set-mention ────────────────────────────────────────────────


async def test_set_mention_sets_the_role_for_every_configured_event_type(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await account_set_mention.callback(interaction, account_id, FakeRole(555), None)  # type: ignore[arg-type]

    config = await interaction.client.alert_configs_repo.get(account_id, "video_published")
    assert config is not None and config["mention_role_id"] == 555
    assert "will now mention" in interaction.response.messages[0]["content"]
    # The confirmation must never ping the role it's describing.
    allowed = interaction.response.messages[0]["allowed_mentions"]
    assert allowed.everyone is False and allowed.roles is False


async def test_set_mention_with_no_role_clears_it(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await account_set_mention.callback(interaction, account_id, FakeRole(555), None)  # type: ignore[arg-type]

    await account_set_mention.callback(interaction, account_id, None, None)  # type: ignore[arg-type]

    config = await interaction.client.alert_configs_repo.get(account_id, "video_published")
    assert config is not None and config["mention_role_id"] is None
    assert "cleared" in interaction.response.messages[-1]["content"].lower()


async def test_set_mention_can_target_one_event_type(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await account_set_mention.callback(  # type: ignore[arg-type]
        interaction, account_id, FakeRole(555), FakeChoice("New Video", "video_published")
    )

    video = await interaction.client.alert_configs_repo.get(account_id, "video_published")
    short = await interaction.client.alert_configs_repo.get(account_id, "short_published")
    assert video is not None and video["mention_role_id"] == 555
    assert short is not None and short["mention_role_id"] is None


async def test_set_mention_for_unconfigured_account_reports_not_found(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_set_mention.callback(  # type: ignore[arg-type]
        interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a", FakeRole(555), None
    )

    assert "isn't configured" in interaction.response.messages[0]["content"]


async def test_set_mention_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_set_mention.callback(interaction, "Youtube: @vectorgames_", FakeRole(555), None)  # type: ignore[arg-type]

    assert "valid account" in interaction.response.messages[0]["content"].lower()


# ── alerts (view + toggle) ──────────────────────────────────────────────


async def test_alerts_with_no_params_shows_current_state(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await alerts.callback(interaction, account_id, FakeChoice("New Video", "video_published"), None, None)  # type: ignore[arg-type]

    content = interaction.response.messages[0]["content"]
    assert "On" in content  # default enabled=True, embed_enabled=True


async def test_alerts_toggles_enabled_only(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await alerts.callback(interaction, account_id, FakeChoice("New Video", "video_published"), False, None)  # type: ignore[arg-type]

    config = await interaction.client.alert_configs_repo.get(account_id, "video_published")
    assert config is not None
    assert config["enabled"] is False
    assert config["embed_enabled"] is True  # untouched


async def test_alerts_toggles_embed_only(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await alerts.callback(interaction, account_id, FakeChoice("New Video", "video_published"), None, False)  # type: ignore[arg-type]

    config = await interaction.client.alert_configs_repo.get(account_id, "video_published")
    assert config is not None
    assert config["enabled"] is True  # untouched
    assert config["embed_enabled"] is False


async def test_alerts_for_unknown_event_type_configuration_reports_not_found(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await alerts.callback(interaction, account_id, FakeChoice("New Post", "post_created"), False, None)  # type: ignore[arg-type]

    assert "doesn't have an alert configuration" in interaction.response.messages[0]["content"]


async def test_alerts_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await alerts.callback(  # type: ignore[arg-type]
        interaction, "Youtube: @vectorgames_", FakeChoice("New Video", "video_published"), False, None
    )

    assert "valid account" in interaction.response.messages[0]["content"].lower()


# ── account-info ────────────────────────────────────────────────────────


async def test_account_info_shows_every_configured_event_type(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    embed = interaction.response.messages[0]["embed"]
    field_names = {field.name for field in embed.fields}
    assert "New Video" in field_names
    assert "New Short" in field_names


async def test_account_info_surfaces_the_custom_message(db: FakeDatabase) -> None:
    """This is the direct fix for 'there's no way to see the custom
    message once it's set' — account-set-message could only write it."""
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_custom_message(
        account_id, "Hey @everyone, {creator} just posted!", event_type="video_published"
    )

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    embed = interaction.response.messages[0]["embed"]
    video_field = next(f for f in embed.fields if f.name == "New Video")
    assert "Hey @everyone, {creator} just posted!" in video_field.value


async def test_account_info_shows_the_mention_role(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_mention_role(account_id, 555, event_type="video_published")

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    embed = interaction.response.messages[0]["embed"]
    video_field = next(f for f in embed.fields if f.name == "New Video")
    assert "<@&555>" in video_field.value


async def test_account_info_shows_the_everyone_role_as_everyone(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_mention_role(account_id, 1, event_type="video_published")

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    embed = interaction.response.messages[0]["embed"]
    video_field = next(f for f in embed.fields if f.name == "New Video")
    assert "@everyone" in video_field.value
    assert "<@&1>" not in video_field.value


async def test_account_info_for_unconfigured_account_reports_not_found(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_info.callback(interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a")  # type: ignore[arg-type]

    assert "isn't configured" in interaction.response.messages[0]["content"]


async def test_account_info_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_info.callback(interaction, "Youtube: @vectorgames_")  # type: ignore[arg-type]

    assert "valid account" in interaction.response.messages[0]["content"].lower()
