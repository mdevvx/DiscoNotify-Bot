"""Section 10's dedup requirement, exercised directly against the repository."""

from __future__ import annotations

import pytest

from database.repositories.events import EventRepository
from tests.unit.fakes import FakeDatabase


@pytest.fixture
def repo() -> EventRepository:
    return EventRepository(FakeDatabase())  # type: ignore[arg-type]


async def test_record_if_new_returns_the_row_on_first_insert(repo: EventRepository) -> None:
    row = await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    assert row is not None
    assert row["platform_event_id"] == "vid-1"
    assert row["notified"] is False


async def test_record_if_new_returns_none_for_an_exact_duplicate(repo: EventRepository) -> None:
    await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    duplicate = await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    assert duplicate is None


async def test_different_event_type_is_not_treated_as_a_duplicate(repo: EventRepository) -> None:
    await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    other = await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="stream_started", platform_event_id="vid-1"
    )
    assert other is not None


async def test_same_video_id_from_two_guilds_watching_the_same_channel_are_independent(repo: EventRepository) -> None:
    """Different guild -> different platform_accounts.id -> different account_id, so
    both get recorded (see the Phase 2 schema notes on per-guild dedup)."""
    first = await repo.record_if_new(
        guild_id=1, account_id="acc-guild-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    second = await repo.record_if_new(
        guild_id=2, account_id="acc-guild-2", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    assert first is not None
    assert second is not None


async def test_mark_notified_sets_notified_flag(repo: EventRepository) -> None:
    row = await repo.record_if_new(
        guild_id=1, account_id="acc-1", platform="youtube", event_type="video_published", platform_event_id="vid-1"
    )
    assert row is not None

    await repo.mark_notified(row["id"])

    fake_db = repo._db  # type: ignore[attr-defined]
    stored = fake_db.tables["notify_events"].rows[0]
    assert stored["notified"] is True
    assert stored["notified_at"] is not None
