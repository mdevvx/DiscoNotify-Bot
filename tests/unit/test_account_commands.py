"""Covers a real production bug: Discord's autocomplete on a plain string
parameter is only a suggestion — nothing stops a user from submitting
arbitrary typed text (e.g. the suggestion's own display label) instead of
picking it, which used to crash straight into a raw Postgres "invalid
input syntax for type uuid" error. account-remove and account-set-message
must both catch that before ever querying the database.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from commands.accounts.account_commands import (
    RemoveAccountConfirmView,
    _is_valid_account_id,
    account_remove,
)
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from tests.unit.fake_interactions import FakeFollowup, FakeResponse, FakeUser
from tests.unit.fakes import FakeDatabase


def test_is_valid_account_id_accepts_a_real_uuid() -> None:
    assert _is_valid_account_id("f95a0f01-b36c-4e5c-b755-376619ffd81a") is True


def test_is_valid_account_id_rejects_an_autocomplete_display_label() -> None:
    """The exact string that caused the crash in production."""
    assert _is_valid_account_id("Youtube: @vectorgames_") is False


def test_is_valid_account_id_rejects_empty_string() -> None:
    assert _is_valid_account_id("") is False


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class FakeClient:
    def __init__(self, db: FakeDatabase) -> None:
        self.accounts_repo = PlatformAccountRepository(db)  # type: ignore[arg-type]
        self.alert_configs_repo = AlertConfigurationRepository(db)  # type: ignore[arg-type]


class FakeInteraction:
    def __init__(self, guild_id: int, db: FakeDatabase, *, user_id: int = 100) -> None:
        self.guild = FakeGuild(guild_id)
        self.client = FakeClient(db)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = FakeUser(user_id)


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


async def test_account_remove_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_remove.callback(interaction, "Youtube: @vectorgames_")  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    assert "valid account" in interaction.response.messages[0]["content"].lower()


async def test_account_remove_with_a_real_but_nonexistent_id_gives_a_friendly_message_not_a_crash(
    db: FakeDatabase,
) -> None:
    interaction = FakeInteraction(1, db)

    await account_remove.callback(interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a")  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    assert "isn't configured" in interaction.response.messages[0]["content"]



# ── account-remove confirmation ─────────────────────────────────────────


def _buttons(view: Any) -> List[Any]:
    import discord

    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


async def _seed_account(interaction: FakeInteraction) -> str:
    row = await interaction.client.accounts_repo.add(interaction.guild.id, "youtube", "UCabc", "@example")
    return row["id"]


async def _ask_to_remove(db: FakeDatabase) -> "tuple[FakeInteraction, str, RemoveAccountConfirmView]":
    interaction = FakeInteraction(1, db)
    account_id = await _seed_account(interaction)
    await account_remove.callback(interaction, account_id)  # type: ignore[arg-type]
    return interaction, account_id, interaction.response.messages[0]["view"]


async def test_account_remove_asks_for_confirmation_and_deletes_nothing_yet(db: FakeDatabase) -> None:
    interaction, account_id, view = await _ask_to_remove(db)

    prompt = interaction.response.messages[0]
    assert isinstance(view, RemoveAccountConfirmView)
    assert [button.label for button in _buttons(view)] == ["Remove", "Cancel"]
    assert prompt["ephemeral"] is True
    assert "@example" in prompt["content"]
    assert await interaction.client.accounts_repo.get_by_guild(1, account_id) is not None


async def test_confirming_removes_the_account_and_announces_it(db: FakeDatabase) -> None:
    interaction, account_id, view = await _ask_to_remove(db)
    click = FakeInteraction(1, db)

    await view.confirm(click)  # type: ignore[arg-type]

    assert await interaction.client.accounts_repo.get_by_guild(1, account_id) is None
    assert not _buttons(click.response.edits[0]["view"])  # buttons gone
    assert "stopped monitoring **@example**" in click.followup.messages[0]["content"]


async def test_cancelling_keeps_the_account(db: FakeDatabase) -> None:
    interaction, account_id, view = await _ask_to_remove(db)
    click = FakeInteraction(1, db)

    await view.cancel(click)  # type: ignore[arg-type]

    assert await interaction.client.accounts_repo.get_by_guild(1, account_id) is not None
    assert "Cancelled" in click.response.edits[0]["content"]
    assert click.followup.messages == []


async def test_only_the_admin_who_ran_the_command_can_confirm(db: FakeDatabase) -> None:
    _, _, view = await _ask_to_remove(db)
    someone_else = FakeInteraction(1, db, user_id=999)

    allowed = await view.interaction_check(someone_else)  # type: ignore[arg-type]

    assert allowed is False
    assert someone_else.response.messages[0]["ephemeral"] is True


async def test_confirming_an_account_already_removed_elsewhere_says_so(db: FakeDatabase) -> None:
    interaction, account_id, view = await _ask_to_remove(db)
    await interaction.client.accounts_repo.remove(1, account_id)
    click = FakeInteraction(1, db)

    await view.confirm(click)  # type: ignore[arg-type]

    assert "already removed" in click.response.edits[0]["content"]
    assert click.followup.messages == []
