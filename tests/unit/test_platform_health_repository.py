"""Backs the Phase 3 API usage manager: per-platform status, failure
thresholds, and the daily request counter."""

from __future__ import annotations

import datetime

import pytest

from database.repositories.platform_health import PlatformHealthRepository
from tests.unit.fakes import FakeDatabase


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def repo(db: FakeDatabase) -> PlatformHealthRepository:
    return PlatformHealthRepository(db)  # type: ignore[arg-type]


async def test_record_success_creates_a_row_on_first_use(repo: PlatformHealthRepository) -> None:
    await repo.record_success("youtube")

    row = await repo.get("youtube")
    assert row is not None
    assert row["status"] == "healthy"
    assert row["consecutive_failures"] == 0


async def test_record_failure_below_thresholds_stays_healthy(repo: PlatformHealthRepository) -> None:
    await repo.record_failure("youtube", "timeout", degrade_after=3, error_after=8)

    row = await repo.get("youtube")
    assert row is not None
    assert row["status"] == "healthy"
    assert row["consecutive_failures"] == 1


async def test_record_failure_reaching_degrade_threshold(repo: PlatformHealthRepository) -> None:
    for _ in range(3):
        await repo.record_failure("youtube", "timeout", degrade_after=3, error_after=8)

    row = await repo.get("youtube")
    assert row is not None
    assert row["status"] == "degraded"


async def test_record_failure_reaching_error_threshold(repo: PlatformHealthRepository) -> None:
    for _ in range(8):
        await repo.record_failure("youtube", "timeout", degrade_after=3, error_after=8)

    row = await repo.get("youtube")
    assert row is not None
    assert row["status"] == "error"


async def test_record_success_after_failures_recovers_to_healthy(repo: PlatformHealthRepository) -> None:
    for _ in range(8):
        await repo.record_failure("youtube", "timeout", degrade_after=3, error_after=8)
    await repo.record_success("youtube")

    row = await repo.get("youtube")
    assert row is not None
    assert row["status"] == "healthy"
    assert row["consecutive_failures"] == 0


async def test_increment_requests_accumulates_within_the_same_day(repo: PlatformHealthRepository) -> None:
    await repo.increment_requests("youtube", by=5)
    await repo.increment_requests("youtube", by=3)

    row = await repo.get("youtube")
    assert row is not None
    assert row["requests_today"] == 8


async def test_increment_requests_resets_on_a_new_day(db: FakeDatabase, repo: PlatformHealthRepository) -> None:
    await repo.increment_requests("youtube", by=5)

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    db.tables["notify_platform_health"].rows[0]["requests_today_reset_at"] = yesterday

    await repo.increment_requests("youtube", by=2)

    row = await repo.get("youtube")
    assert row is not None
    assert row["requests_today"] == 2
    assert row["requests_today_reset_at"] == datetime.date.today().isoformat()


async def test_platforms_have_independent_health(repo: PlatformHealthRepository) -> None:
    await repo.record_failure("youtube", "timeout", degrade_after=3, error_after=8)
    await repo.record_success("twitch")

    youtube = await repo.get("youtube")
    twitch = await repo.get("twitch")
    assert youtube is not None and youtube["status"] == "healthy"  # only 1 failure, below degrade_after
    assert twitch is not None and twitch["status"] == "healthy"
