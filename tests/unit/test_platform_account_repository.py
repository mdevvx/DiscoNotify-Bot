"""PlatformAccountRepository backs the scheduler's "what do I poll" query
and section 34's failure classification. Rows are seeded directly into the
fake table here rather than through an add-account command, to keep these
tests focused on the repository's own logic in isolation."""

from __future__ import annotations

import pytest

from database.repositories.accounts import PlatformAccountRepository
from tests.unit.fakes import FakeDatabase


def _seed(db: FakeDatabase, **overrides) -> dict:
    row = {
        "id": "acc-1",
        "guild_id": 1,
        "platform": "youtube",
        "platform_account_id": "UC123",
        "username": "example",
        "display_name": None,
        "enabled": True,
        "status": "active",
        "last_checked_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "content_cursor": None,
    }
    row.update(overrides)
    db.tables["notify_platform_accounts"].rows.append(row)
    return row


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def repo(db: FakeDatabase) -> PlatformAccountRepository:
    return PlatformAccountRepository(db)  # type: ignore[arg-type]


async def test_list_enabled_by_platform_filters_platform_and_enabled(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", platform="youtube", enabled=True)
    _seed(db, id="acc-2", platform="youtube", enabled=False)
    _seed(db, id="acc-3", platform="twitch", enabled=True)

    rows = await repo.list_enabled_by_platform("youtube")

    assert [r["id"] for r in rows] == ["acc-1"]


async def test_record_success_resets_failures_and_status(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", status="error", consecutive_failures=12, last_error="boom")

    await repo.record_success("acc-1")

    row = await repo.get("acc-1")
    assert row is not None
    assert row["status"] == "active"
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None


async def test_record_failure_increments_consecutive_failures(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", consecutive_failures=2)

    await repo.record_failure("acc-1", "timeout")

    row = await repo.get("acc-1")
    assert row is not None
    assert row["consecutive_failures"] == 3
    assert row["status"] == "active"  # below the error threshold


async def test_record_failure_flips_to_error_after_persistent_failures(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", consecutive_failures=9)  # one more reaches the threshold (10)

    await repo.record_failure("acc-1", "timeout")

    row = await repo.get("acc-1")
    assert row is not None
    assert row["status"] == "error"


async def test_record_failure_honors_explicit_invalid_status(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", consecutive_failures=0)

    await repo.record_failure("acc-1", "account not found", status="invalid")

    row = await repo.get("acc-1")
    assert row is not None
    assert row["status"] == "invalid"


async def test_set_content_cursor_persists_across_reads(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", content_cursor=None)

    await repo.set_content_cursor("acc-1", "video-42")

    row = await repo.get("acc-1")
    assert row is not None
    assert row["content_cursor"] == "video-42"


# ── add / remove / list (the /account command group's repository layer) ──


async def test_add_creates_a_new_account(repo: PlatformAccountRepository) -> None:
    row = await repo.add(1, "youtube", "UC123", "Example Creator")

    assert row is not None
    assert row["guild_id"] == 1
    assert row["platform"] == "youtube"
    assert row["platform_account_id"] == "UC123"


async def test_add_prevents_duplicate_account_in_the_same_guild(repo: PlatformAccountRepository) -> None:
    first = await repo.add(1, "youtube", "UC123", "Example Creator")
    duplicate = await repo.add(1, "youtube", "UC123", "Example Creator")

    assert first is not None
    assert duplicate is None


async def test_add_allows_the_same_account_in_two_different_guilds(repo: PlatformAccountRepository) -> None:
    first = await repo.add(1, "youtube", "UC123", "Example Creator")
    second = await repo.add(2, "youtube", "UC123", "Example Creator")

    assert first is not None
    assert second is not None


async def test_add_allows_two_different_accounts_in_the_same_guild(repo: PlatformAccountRepository) -> None:
    first = await repo.add(1, "youtube", "UC123", "Creator A")
    second = await repo.add(1, "youtube", "UC456", "Creator B")

    assert first is not None
    assert second is not None


async def test_list_by_guild_only_returns_that_guilds_accounts(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", guild_id=1)
    _seed(db, id="acc-2", guild_id=2)

    accounts = await repo.list_by_guild(1)

    assert [a["id"] for a in accounts] == ["acc-1"]


async def test_get_by_guild_returns_none_for_a_different_guilds_account(
    db: FakeDatabase, repo: PlatformAccountRepository
) -> None:
    _seed(db, id="acc-1", guild_id=1)

    assert await repo.get_by_guild(2, "acc-1") is None
    assert await repo.get_by_guild(1, "acc-1") is not None


async def test_remove_deletes_the_account_and_returns_true(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", guild_id=1)

    removed = await repo.remove(1, "acc-1")

    assert removed is True
    assert await repo.get("acc-1") is None


async def test_remove_cannot_delete_another_guilds_account(db: FakeDatabase, repo: PlatformAccountRepository) -> None:
    _seed(db, id="acc-1", guild_id=1)

    removed = await repo.remove(2, "acc-1")  # wrong guild_id

    assert removed is False
    assert await repo.get("acc-1") is not None  # still there
