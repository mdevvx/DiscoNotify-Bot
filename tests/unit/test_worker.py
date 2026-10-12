"""AccountWorker is where most of the framework's actual behavior lives:
new-content detection, live-transition detection (section 8: exactly one
notification per transition, including across a simulated restart), and
error isolation (section 9: one account's failure must not raise past
poll_once()). Driven through a FakePlatformAdapter rather than a mock, so
these tests exercise the real PlatformAdapter contract."""

from __future__ import annotations

import datetime
from typing import Any, List, Optional

import pytest

from database.repositories.accounts import PlatformAccountRepository
from database.repositories.live_states import LiveStateRepository
from database.repositories.platform_health import PlatformHealthRepository
from events.models import EventType, NormalizedEvent
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from monitoring.worker import AccountWorker
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from tests.unit.fakes import FakeDatabase
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError


class FakeAdapter(PlatformAdapter):
    platform = "fake"
    capabilities = frozenset({Capability.VIDEOS, Capability.LIVE_STATUS})

    def __init__(self) -> None:
        self.pending_items: List[dict] = []
        self.live_queue: List[LiveStatus] = []
        self.fetch_error: Optional[Exception] = None
        self.live_error: Optional[Exception] = None
        self.fetch_calls = 0
        self.live_calls = 0
        self.seen_cursors: List[Optional[str]] = []

    async def validate_account(self, identifier: str) -> AccountRef:
        raise NotImplementedError

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        self.fetch_calls += 1
        self.seen_cursors.append(cursor)
        if self.fetch_error is not None:
            raise self.fetch_error
        items, self.pending_items = self.pending_items, []
        return FetchResult(items=items, next_cursor="next-cursor")

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        self.live_calls += 1
        if self.live_error is not None:
            raise self.live_error
        if self.live_queue:
            return self.live_queue.pop(0)
        return LiveStatus(is_live=False)

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        return NormalizedEvent(
            platform=account.platform,
            platform_account_id=account.platform_account_id,
            account_username=account.username,
            event_type=EventType.VIDEO_PUBLISHED,
            platform_event_id=raw_item["id"],
            title=raw_item.get("title"),
        )


def _seed_account(db: FakeDatabase, account_id: str = "acc-1", **overrides: Any) -> None:
    row = {
        "id": account_id,
        "guild_id": 1,
        "platform": "fake",
        "platform_account_id": f"ext-{account_id}",
        "username": "creator",
        "enabled": True,
        "status": "active",
        "last_checked_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "content_cursor": None,
    }
    row.update(overrides)
    db.tables["pulsenotify_platform_accounts"].rows.append(row)


class Harness:
    def __init__(self, initial_cursor: Optional[str] = None) -> None:
        self.db = FakeDatabase()
        _seed_account(self.db, content_cursor=initial_cursor)
        self.adapter = FakeAdapter()
        self.account_repo = PlatformAccountRepository(self.db)  # type: ignore[arg-type]
        self.live_state_repo = LiveStateRepository(self.db)  # type: ignore[arg-type]
        self.health_tracker = PlatformHealthTracker(
            "fake", PlatformHealthRepository(self.db), degrade_after=3, error_after=8  # type: ignore[arg-type]
        )
        self.submitted: List[NormalizedEvent] = []
        self.worker = AccountWorker(
            guild_id=1,
            account_id="acc-1",
            account_ref=AccountRef(platform="fake", platform_account_id="ext-1", username="creator"),
            adapter=self.adapter,
            account_repo=self.account_repo,
            live_state_repo=self.live_state_repo,
            rate_limiter=RateLimiter(max_requests=1000, period_seconds=1.0),
            retry_policy=RetryPolicy(max_attempts=2, base_delay=0.001, max_delay=0.01, jitter_fraction=0.0),
            health_tracker=self.health_tracker,
            submit_event=self._submit,
            initial_cursor=initial_cursor,
        )

    async def _submit(self, event: NormalizedEvent) -> None:
        self.submitted.append(event)


@pytest.fixture
def harness() -> Harness:
    return Harness()


async def test_new_content_is_normalized_and_submitted(harness: Harness) -> None:
    harness.adapter.pending_items = [{"id": "vid-1", "title": "First video"}]

    await harness.worker.poll_once()

    assert len(harness.submitted) == 1
    event = harness.submitted[0]
    assert event.platform_event_id == "vid-1"
    assert event.guild_id == 1
    assert event.account_id == "acc-1"


async def test_cursor_is_carried_between_polls(harness: Harness) -> None:
    await harness.worker.poll_once()
    await harness.worker.poll_once()

    assert harness.adapter.seen_cursors == [None, "next-cursor"]


async def test_successful_content_poll_persists_the_new_cursor(harness: Harness) -> None:
    await harness.worker.poll_once()

    row = await harness.account_repo.get("acc-1")
    assert row is not None
    assert row["content_cursor"] == "next-cursor"


async def test_worker_resumes_from_a_persisted_cursor_instead_of_restarting_at_none() -> None:
    """A bot restart must not reset an existing account back to a fresh
    'establish a baseline' state — that's what the persisted cursor from
    migration 0003 exists to prevent."""
    harness = Harness(initial_cursor="already-seen-cursor")

    await harness.worker.poll_once()

    assert harness.adapter.seen_cursors == ["already-seen-cursor"]


async def test_offline_to_live_fires_exactly_one_stream_started(harness: Harness) -> None:
    harness.adapter.live_queue = [LiveStatus(is_live=True, stream_id="s1", title="Live now")]

    await harness.worker.poll_once()

    started = [e for e in harness.submitted if e.event_type == EventType.STREAM_STARTED]
    assert len(started) == 1
    assert started[0].platform_event_id == "s1"


async def test_stream_started_carries_the_details_a_rich_embed_needs(harness: Harness) -> None:
    harness.adapter.live_queue = [
        LiveStatus(
            is_live=True,
            stream_id="s1",
            title="Live now",
            description="Join us",
            author="Vector Games",
            author_avatar_url="https://yt3.example/a.jpg",
        )
    ]

    await harness.worker.poll_once()

    started = next(e for e in harness.submitted if e.event_type == EventType.STREAM_STARTED)
    assert started.description == "Join us"
    assert started.author == "Vector Games"
    assert started.author_avatar_url == "https://yt3.example/a.jpg"


async def test_still_live_on_next_poll_does_not_fire_again(harness: Harness) -> None:
    harness.adapter.live_queue = [
        LiveStatus(is_live=True, stream_id="s1"),
        LiveStatus(is_live=True, stream_id="s1"),
    ]

    await harness.worker.poll_once()
    await harness.worker.poll_once()

    started = [e for e in harness.submitted if e.event_type == EventType.STREAM_STARTED]
    assert len(started) == 1


async def test_live_to_offline_fires_stream_ended(harness: Harness) -> None:
    harness.adapter.live_queue = [
        LiveStatus(is_live=True, stream_id="s1"),
        LiveStatus(is_live=False),
    ]

    await harness.worker.poll_once()
    await harness.worker.poll_once()

    ended = [e for e in harness.submitted if e.event_type == EventType.STREAM_ENDED]
    assert len(ended) == 1


async def test_two_separate_live_sessions_without_a_stream_id_get_different_event_ids(harness: Harness) -> None:
    """Regression test: a platform whose LiveStatus never carries a
    stream_id (e.g. Kick) must not have its synthetic fallback ID collide
    across two different go-live sessions of the same account — that would
    silently dedup every STREAM_STARTED after the first one forever, since
    the events table's unique key is (account_id, event_type,
    platform_event_id)."""
    harness.adapter.live_queue = [
        LiveStatus(is_live=True, stream_id=None, started_at=datetime.datetime(2026, 1, 1)),
        LiveStatus(is_live=False),
        LiveStatus(is_live=True, stream_id=None, started_at=datetime.datetime(2026, 1, 2)),
    ]

    await harness.worker.poll_once()  # goes live (session 1)
    await harness.worker.poll_once()  # goes offline
    await harness.worker.poll_once()  # goes live again (session 2)

    started = [e for e in harness.submitted if e.event_type == EventType.STREAM_STARTED]
    assert len(started) == 2
    assert started[0].platform_event_id != started[1].platform_event_id


async def test_restart_with_already_live_state_does_not_reannounce(harness: Harness) -> None:
    """Simulates a bot restart while a streamer is already live: the
    live_states row already says is_live=True before the worker's first
    poll of this process."""
    await harness.live_state_repo.set_live(guild_id=1, account_id="acc-1", stream_id="s1", started_at=None)
    harness.adapter.live_queue = [LiveStatus(is_live=True, stream_id="s1")]

    await harness.worker.poll_once()

    started = [e for e in harness.submitted if e.event_type == EventType.STREAM_STARTED]
    assert started == []


async def test_transient_failure_is_recorded_and_does_not_raise(harness: Harness) -> None:
    harness.adapter.fetch_error = TransientPlatformError("timeout")

    await harness.worker.poll_once()  # must not raise

    row = await harness.account_repo.get("acc-1")
    assert row is not None
    assert row["consecutive_failures"] == 1
    assert row["status"] == "active"  # below the error threshold


async def test_permanent_failure_marks_account_invalid_without_retrying(harness: Harness) -> None:
    harness.adapter.fetch_error = PermanentPlatformError("account deleted")

    await harness.worker.poll_once()

    assert harness.adapter.fetch_calls == 1  # no retries for a permanent error
    row = await harness.account_repo.get("acc-1")
    assert row is not None
    assert row["status"] == "invalid"


async def test_success_after_failure_resets_status(harness: Harness) -> None:
    harness.adapter.fetch_error = TransientPlatformError("timeout")
    await harness.worker.poll_once()

    harness.adapter.fetch_error = None
    await harness.worker.poll_once()

    row = await harness.account_repo.get("acc-1")
    assert row is not None
    assert row["consecutive_failures"] == 0
    assert row["status"] == "active"


async def test_rate_limited_error_pauses_the_platform_not_the_account(harness: Harness) -> None:
    harness.adapter.fetch_error = RateLimitedError("429 too many requests", retry_after=42.0)

    await harness.worker.poll_once()  # must not raise

    assert harness.adapter.fetch_calls == 1  # RetryPolicy doesn't retry a rate limit either
    assert harness.health_tracker.is_rate_limited() is True

    # Not the account's fault — its own failure count is untouched.
    row = await harness.account_repo.get("acc-1")
    assert row is not None
    assert row["consecutive_failures"] == 0
    assert row["status"] == "active"
