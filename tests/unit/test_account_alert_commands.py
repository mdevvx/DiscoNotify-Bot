"""Per-account alert configuration: the account-set-mention,
account-set-message and alerts commands (each opens a modal form —
commands/accounts/modals.py), and account-info (full config, including
seeing a previously-set custom message)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from commands.accounts.account_commands import account_info, account_set_mention, account_set_message, alerts
from commands.accounts.modals import AlertsModal, CustomMessageModal, MentionModal
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from platforms.base import event_types_for_capabilities, Capability
from tests.unit.fake_interactions import FakeFollowup, FakeResponse, FakeUser
from tests.unit.fakes import FakeDatabase


class FakeRole:
    def __init__(self, role_id: int, *, mentionable: bool) -> None:
        self.id = role_id
        self.name = "Staff"
        self.mentionable = mentionable


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.roles = {555: FakeRole(555, mentionable=True), 777: FakeRole(777, mentionable=False)}

    def get_role(self, role_id: int) -> Any:
        return self.roles.get(role_id)


class FakeClient:
    def __init__(self, db: FakeDatabase) -> None:
        self.accounts_repo = PlatformAccountRepository(db)  # type: ignore[arg-type]
        self.alert_configs_repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]


class FakeInteraction:
    def __init__(self, guild_id: int, db: FakeDatabase) -> None:
        self.guild = FakeGuild(guild_id)
        self.client = FakeClient(db)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = FakeUser(100)


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


async def _seed_account_with_alerts(interaction: FakeInteraction, guild_id: int = 1) -> str:
    row = await interaction.client.accounts_repo.add(guild_id, "youtube", "UCabc", "SomeCreator")
    assert row is not None
    event_types = event_types_for_capabilities(frozenset({Capability.VIDEOS, Capability.SHORTS}))
    await interaction.client.alert_configs_repo.seed_defaults(guild_id, row["id"], [t.value for t in event_types])
    return row["id"]


async def _open_modal(command: Any, db: FakeDatabase, **seed: Any) -> "tuple[FakeInteraction, str, Any]":
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    for event_type, role_id in seed.get("roles", {}).items():
        await interaction.client.alert_configs_repo.set_mention_role(account_id, role_id, event_type=event_type)
    await command.callback(interaction, account_id)  # type: ignore[arg-type]
    modal = interaction.response.modals[0]
    # Submitting the form is a new interaction in Discord, with its own response.
    interaction.response = FakeResponse()
    return interaction, account_id, modal


def _default_options(select: Any) -> List[str]:
    return [option.value for option in select.options if option.default]


async def _config(interaction: FakeInteraction, account_id: str, event_type: str) -> Dict[str, Any]:
    config = await interaction.client.alert_configs_repo.get(account_id, event_type)
    assert config is not None
    return config


def _field(interaction: FakeInteraction, name: str) -> str:
    """One field's value from a card reply — fields are text blocks
    rendered as "**name**" over the value (ui/cards.py)."""
    import discord

    heading = f"**{name}**\n"
    for item in interaction.response.messages[0]["view"].walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith(heading):
            return item.content[len(heading):]
    raise AssertionError(f"no {name!r} field")


# ── shared command behavior ─────────────────────────────────────────────


@pytest.mark.parametrize("command", [account_set_mention, account_set_message, alerts])
async def test_a_display_label_instead_of_an_id_does_not_crash(command: Any, db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await command.callback(interaction, "Youtube: @vectorgames_")  # type: ignore[arg-type]

    assert "valid account" in interaction.response.messages[0]["content"].lower()
    assert interaction.response.modals == []


@pytest.mark.parametrize("command", [account_set_mention, account_set_message, alerts])
async def test_an_unknown_account_does_not_open_a_form(command: Any, db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await command.callback(interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a")  # type: ignore[arg-type]

    assert "isn't configured" in interaction.response.messages[0]["content"]
    assert interaction.response.modals == []


# ── account-set-mention ────────────────────────────────────────────────


async def test_set_mention_opens_a_form_prefilled_with_the_current_role(db: FakeDatabase) -> None:
    _, _, modal = await _open_modal(
        account_set_mention, db, roles={"video_published": 555, "short_published": 555}
    )

    assert isinstance(modal, MentionModal)
    assert _default_options(modal.mode) == ["role"]
    assert [value.id for value in modal.role.default_values] == [555]


async def test_set_mention_form_shows_everyone_as_its_own_choice(db: FakeDatabase) -> None:
    _, _, modal = await _open_modal(account_set_mention, db, roles={"video_published": 1, "short_published": 1})

    assert _default_options(modal.mode) == ["everyone"]


async def test_set_mention_applies_a_role_to_every_alert_type(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_mention, db)

    await modal.apply(interaction, mode="role", role_id=555, event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] == 555
    assert (await _config(interaction, account_id, "short_published"))["mention_role_id"] == 555
    assert "<@&555>" in interaction.response.messages[0]["content"]


async def test_set_mention_everyone_confirms_with_a_single_at_sign(db: FakeDatabase) -> None:
    """Regression test: the confirmation used Role.mention, which renders
    the @everyone role as `<@&guild_id>` — shown in Discord as a dead
    "@@everyone"."""
    interaction, account_id, modal = await _open_modal(account_set_mention, db)

    await modal.apply(interaction, mode="everyone", role_id=None, event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] == 1  # the guild's ID
    confirmation = interaction.response.messages[0]
    assert "@everyone" in confirmation["content"]
    assert "<@&1>" not in confirmation["content"]
    assert confirmation["allowed_mentions"].everyone is False  # the confirmation itself never pings


@pytest.mark.parametrize(("mode", "role_id"), [("everyone", None), ("role", 777)])
async def test_only_members_who_may_ping_everyone_can_set_everyone_or_a_locked_role(
    db: FakeDatabase, mode: str, role_id: Any
) -> None:
    """The bot has Mention Everyone, so a Manage Server member without it
    mustn't be able to make the bot mass-ping on their behalf."""
    from tests.unit.fake_interactions import FakeUser

    interaction, account_id, modal = await _open_modal(account_set_mention, db)
    interaction.user = FakeUser(100, mention_everyone=False)

    await modal.apply(interaction, mode=mode, role_id=role_id, event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] is None
    assert "Mention @everyone, @here and All Roles" in interaction.response.messages[0]["content"]


async def test_a_normal_mentionable_role_needs_no_extra_permission(db: FakeDatabase) -> None:
    from tests.unit.fake_interactions import FakeUser

    interaction, account_id, modal = await _open_modal(account_set_mention, db)
    interaction.user = FakeUser(100, mention_everyone=False)

    await modal.apply(interaction, mode="role", role_id=555, event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] == 555


async def test_a_custom_message_with_here_needs_the_permission(db: FakeDatabase) -> None:
    from tests.unit.fake_interactions import FakeUser

    interaction, account_id, modal = await _open_modal(account_set_message, db)
    interaction.user = FakeUser(100, mention_everyone=False)

    await modal.apply(interaction, message="@here {creator} is live!", event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["custom_message"] is None
    assert "Mention @everyone" in interaction.response.messages[0]["content"]


async def test_set_mention_nobody_clears_it(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_mention, db, roles={"video_published": 555})

    await modal.apply(interaction, mode="none", role_id=555, event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] is None


async def test_set_mention_can_target_one_event_type(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_mention, db)

    await modal.apply(interaction, mode="role", role_id=555, event_type="short_published")

    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] is None
    assert (await _config(interaction, account_id, "short_published"))["mention_role_id"] == 555


async def test_set_mention_role_mode_without_a_role_asks_for_one(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_mention, db)

    await modal.apply(interaction, mode="role", role_id=None, event_type="__all__")

    assert interaction.response.messages[0]["ephemeral"] is True
    assert (await _config(interaction, account_id, "video_published"))["mention_role_id"] is None


# ── account-set-message ────────────────────────────────────────────────


async def test_set_message_form_prefills_the_current_message(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_custom_message(account_id, "Hi {creator}\n{url}")

    await account_set_message.callback(interaction, account_id)  # type: ignore[arg-type]

    modal = interaction.response.modals[0]
    assert isinstance(modal, CustomMessageModal)
    assert modal.message.default == "Hi {creator}\n{url}"


async def test_set_message_supports_multiple_lines_and_shows_a_preview(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_message, db)

    await modal.apply(interaction, message="New from {creator}!\n{url}", event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["custom_message"] == "New from {creator}!\n{url}"
    assert "New from SomeCreator!\nhttps://example.com/preview-link" in interaction.response.messages[0]["content"]


async def test_set_message_left_empty_restores_the_default(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_message, db)
    await modal.apply(interaction, message="custom", event_type="__all__")

    await modal.apply(interaction, message="   ", event_type="__all__")

    assert (await _config(interaction, account_id, "video_published"))["custom_message"] is None


async def test_set_message_can_target_one_event_type(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(account_set_message, db)

    await modal.apply(interaction, message="Short!", event_type="short_published")

    assert (await _config(interaction, account_id, "video_published"))["custom_message"] is None
    assert (await _config(interaction, account_id, "short_published"))["custom_message"] == "Short!"


# ── alerts ─────────────────────────────────────────────────────────────


async def test_alerts_form_lists_the_accounts_alert_types_prefilled(db: FakeDatabase) -> None:
    interaction, account_id, _ = await _open_modal(alerts, db)
    await interaction.client.alert_configs_repo.set_alert_toggle(account_id, "short_published", enabled=False)
    await alerts.callback(interaction, account_id)  # type: ignore[arg-type]
    modal = interaction.response.modals[-1]

    assert isinstance(modal, AlertsModal)
    assert {option.value for option in modal.enabled.options} == {"video_published", "short_published"}
    assert _default_options(modal.enabled) == ["video_published"]
    assert set(_default_options(modal.embeds)) == {"video_published", "short_published"}


async def test_alerts_applies_every_toggle_at_once(db: FakeDatabase) -> None:
    interaction, account_id, modal = await _open_modal(alerts, db)

    await modal.apply(interaction, enabled=["short_published"], embeds=["video_published"])

    video = await _config(interaction, account_id, "video_published")
    short = await _config(interaction, account_id, "short_published")
    assert (video["enabled"], video["embed_enabled"]) == (False, True)
    assert (short["enabled"], short["embed_enabled"]) == (True, False)
    assert "New Short** — ✅ On · embed off" in interaction.response.messages[0]["content"]


# ── account-info ────────────────────────────────────────────────────────


async def test_account_info_shows_every_configured_event_type(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    text = interaction.response.messages[0]["content"]
    assert "**New Video**" in text
    assert "**New Short**" in text


async def test_account_info_surfaces_the_custom_message(db: FakeDatabase) -> None:
    """This is the direct fix for 'there's no way to see the custom
    message once it's set' — account-set-message could only write it."""
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_custom_message(
        account_id, "Hey @everyone, {creator} just posted!", event_type="video_published"
    )

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    video_field = _field(interaction, "New Video")
    assert "Hey @everyone, {creator} just posted!" in video_field


async def test_account_info_shows_the_mention_role(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_mention_role(account_id, 555, event_type="video_published")

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    video_field = _field(interaction, "New Video")
    assert "<@&555>" in video_field


async def test_account_info_shows_the_everyone_role_as_everyone(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account_with_alerts(interaction)
    await interaction.client.alert_configs_repo.set_mention_role(account_id, 1, event_type="video_published")

    await account_info.callback(interaction, account_id)  # type: ignore[arg-type]

    video_field = _field(interaction, "New Video")
    assert "@everyone" in video_field
    assert "<@&1>" not in video_field


async def test_account_info_for_unconfigured_account_reports_not_found(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_info.callback(interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a")  # type: ignore[arg-type]

    assert "isn't configured" in interaction.response.messages[0]["content"]


async def test_account_info_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_info.callback(interaction, "Youtube: @vectorgames_")  # type: ignore[arg-type]

    assert "valid account" in interaction.response.messages[0]["content"].lower()
