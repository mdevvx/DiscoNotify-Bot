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

from commands.accounts.account_commands import _is_valid_account_id, account_remove, account_set_message
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from tests.unit.fakes import FakeDatabase


def test_is_valid_account_id_accepts_a_real_uuid() -> None:
    assert _is_valid_account_id("f95a0f01-b36c-4e5c-b755-376619ffd81a") is True


def test_is_valid_account_id_rejects_an_autocomplete_display_label() -> None:
    """The exact string that caused the crash in production."""
    assert _is_valid_account_id("Youtube: @vectorgames_") is False


def test_is_valid_account_id_rejects_empty_string() -> None:
    assert _is_valid_account_id("") is False


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


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


async def test_account_remove_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_remove.callback(interaction, "Youtube: @vectorgames_")  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    assert "valid account" in interaction.response.messages[0]["content"].lower()


async def test_account_set_message_with_a_display_label_instead_of_an_id_does_not_crash(db: FakeDatabase) -> None:
    interaction = FakeInteraction(1, db)

    await account_set_message.callback(interaction, "Youtube: @vectorgames_", "hello", None)  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    assert "valid account" in interaction.response.messages[0]["content"].lower()


async def test_account_remove_with_a_real_but_nonexistent_id_gives_a_friendly_message_not_a_crash(
    db: FakeDatabase,
) -> None:
    interaction = FakeInteraction(1, db)

    await account_remove.callback(interaction, "f95a0f01-b36c-4e5c-b755-376619ffd81a")  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    assert "isn't configured" in interaction.response.messages[0]["content"]
