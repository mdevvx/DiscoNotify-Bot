"""Guild isolation and configuration retrieval for GuildRepository.

Section 42 of the project spec calls out "guild isolation" and
"configuration retrieval" as required database tests — these are the
Phase 1 equivalent, since accounts/channels/alert config don't exist yet.
"""

from __future__ import annotations

import pytest

from database.repositories.guilds import GuildRepository
from tests.unit.fakes import FakeDatabase


@pytest.fixture
def repo() -> GuildRepository:
    return GuildRepository(FakeDatabase())  # type: ignore[arg-type]


async def test_new_guild_defaults_to_enabled(repo: GuildRepository) -> None:
    assert await repo.is_enabled(123) is True


async def test_set_enabled_persists_and_is_reflected_immediately(repo: GuildRepository) -> None:
    await repo.set_enabled(123, False)
    assert await repo.is_enabled(123) is False

    await repo.set_enabled(123, True)
    assert await repo.is_enabled(123) is True


async def test_disabling_one_guild_does_not_affect_another(repo: GuildRepository) -> None:
    await repo.set_enabled(111, False)

    assert await repo.is_enabled(111) is False
    assert await repo.is_enabled(222) is True


async def test_separate_repository_instances_see_the_same_underlying_state() -> None:
    db = FakeDatabase()
    repo_a = GuildRepository(db)  # type: ignore[arg-type]
    repo_b = GuildRepository(db)  # type: ignore[arg-type]

    await repo_a.set_enabled(123, False)

    assert await repo_a.is_enabled(123) is False
    assert await repo_b.is_enabled(123) is False


async def test_is_enabled_only_creates_one_row_per_guild(repo: GuildRepository) -> None:
    await repo.is_enabled(123)
    await repo.is_enabled(123)

    fake_db = repo._db  # type: ignore[attr-defined]
    assert len(fake_db.tables["notify_guilds"].rows) == 1
