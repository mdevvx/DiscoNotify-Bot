"""Staying inside platform quotas: no calls while a platform is paused,
fewer retries for quota-bound platforms, polls spread over the interval,
Instagram's budget following Meta's own usage figure and escalating its
pauses, and YouTube's per-Pacific-day quota that survives a restart."""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from database.repositories.accounts import PlatformAccountRepository
from database.repositories.live_states import LiveStateRepository
from database.repositories.platform_health import PlatformHealthRepository
from monitoring.daily_quota import DailyQuota, pacific_date, pacific_offset, seconds_until_pacific_midnight
from monitoring.health import PlatformHealthTracker
from monitoring.rate_limiter import RateLimiter
from monitoring.retry import RetryPolicy
from monitoring.scheduler import PlatformScheduler
from platforms.base import AccountRef
from platforms.instagram.adapter import InstagramAdapter
from platforms.instagram.client import InstagramClient
from platforms.youtube.adapter import YouTubeAdapter
from platforms.youtube.client import QuotaExceededError, YouTubeClient
from tests.unit.fakes import FakeDatabase
from tests.unit.test_instagram_adapter import FakeInstagramClient, _make_adapter, _media, _posted
from tests.unit.test_worker import FakeAdapter, Harness, _seed_account
from utils.errors import PlatformPaused, RateLimitedError, TransientPlatformError

_UTC = datetime.timezone.utc


# ── retry policy ────────────────────────────────────────────────────────


async def test_a_call_can_ask_for_fewer_attempts_than_the_policy_allows() -> None:
    calls = 0

    async def failing() -> None:
        nonlocal calls
        calls += 1
        raise TransientPlatformError("timeout")

    policy = RetryPolicy(max_attempts=5, base_delay=0.001, max_delay=0.01, jitter_fraction=0.0)

    with pytest.raises(TransientPlatformError):
        await policy.run(failing, max_attempts=2)

    assert calls == 2


async def test_a_paused_platform_is_never_retried() -> None:
    calls = 0

    async def paused() -> None:
        nonlocal calls
        calls += 1
        raise PlatformPaused("instagram")

    with pytest.raises(PlatformPaused):
        await RetryPolicy(max_attempts=5, base_delay=0.001, jitter_fraction=0.0).run(paused)

    assert calls == 1


# ── workers stop when the platform is paused ────────────────────────────


@pytest.fixture
def harness() -> Harness:
    return Harness()


async def test_a_paused_platform_gets_no_call_and_records_nothing(harness: Harness) -> None:
    await harness.health_tracker.record_rate_limited(60.0)

    await harness.worker.poll_once()

    assert harness.adapter.fetch_calls == 0 and harness.adapter.live_calls == 0
    row = harness.db.tables["notify_platform_accounts"].rows[0]
    assert row["consecutive_failures"] == 0 and row["last_error"] is None


async def test_a_retry_is_not_made_once_the_platform_has_been_paused(harness: Harness) -> None:
    """The first attempt times out; while it backs off, another worker gets rate limited."""
    harness.adapter.fetch_error = TransientPlatformError("timeout")
    original = harness.adapter.fetch_updates

    async def fetch_then_pause(account: Any, cursor: Any) -> Any:
        try:
            return await original(account, cursor)
        finally:
            await harness.health_tracker.record_rate_limited(60.0)

    harness.adapter.fetch_updates = fetch_then_pause  # type: ignore[method-assign]

    await harness.worker.poll_once()

    assert harness.adapter.fetch_calls == 1  # no second attempt


class _QuotaBoundAdapter(FakeAdapter):
    max_retry_attempts = 1


async def test_the_adapters_own_retry_limit_wins_over_the_default(harness: Harness) -> None:
    harness.worker._adapter = _QuotaBoundAdapter()  # type: ignore[attr-defined]
    harness.worker._adapter.fetch_error = TransientPlatformError("timeout")  # type: ignore[attr-defined]

    await harness.worker.poll_once()

    assert harness.worker._adapter.fetch_calls == 1  # type: ignore[attr-defined]  (the policy alone would try twice)


# ── polls are spread out ────────────────────────────────────────────────


class _SpreadAdapter(FakeAdapter):
    spread_polls = True

    def __init__(self) -> None:
        super().__init__()
        self.fetch_times: List[float] = []

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> Any:
        self.fetch_times.append(asyncio.get_running_loop().time())
        return await super().fetch_updates(account, cursor)


def _scheduler(db: FakeDatabase, adapter: FakeAdapter, *, poll_interval: float) -> PlatformScheduler:
    return PlatformScheduler(
        adapter,
        poll_interval_seconds=poll_interval,
        account_repo=PlatformAccountRepository(db),  # type: ignore[arg-type]
        live_state_repo=LiveStateRepository(db),  # type: ignore[arg-type]
        rate_limiter=RateLimiter(max_requests=1000, period_seconds=1.0),
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.001, max_delay=0.01, jitter_fraction=0.0),
        health_tracker=PlatformHealthTracker("fake", PlatformHealthRepository(db), degrade_after=3, error_after=8),  # type: ignore[arg-type]
        submit_event=lambda event: None,  # type: ignore[arg-type, return-value]
    )


def test_spread_gap_is_half_the_interval_shared_out_and_capped() -> None:
    db = FakeDatabase()
    spread = _scheduler(db, _SpreadAdapter(), poll_interval=200.0)
    plain = _scheduler(db, FakeAdapter(), poll_interval=200.0)

    assert spread._spread_gap(10) == 10.0  # 100 s over 10 accounts
    assert spread._spread_gap(2) == 30.0  # capped
    assert spread._spread_gap(1) == 0.0
    assert plain._spread_gap(10) == 0.0  # not asked for


async def test_a_spread_platform_starts_its_checks_one_after_another() -> None:
    db = FakeDatabase()
    for index in range(3):
        _seed_account(db, f"acc-{index}")
    adapter = _SpreadAdapter()
    scheduler = _scheduler(db, adapter, poll_interval=0.6)  # gap = 0.6 * 0.5 / 3 = 0.1 s

    await scheduler._poll_all()

    gaps = [later - earlier for earlier, later in zip(adapter.fetch_times, adapter.fetch_times[1:])]
    assert len(gaps) == 2 and all(gap >= 0.08 for gap in gaps)


async def test_one_rate_limit_stops_the_accounts_still_waiting_in_line() -> None:
    """The bug this fixes: all accounts called at once, so all were refused at once."""
    db = FakeDatabase()
    for index in range(4):
        _seed_account(db, f"acc-{index}")
    adapter = _SpreadAdapter()
    adapter.fetch_error = RateLimitedError("(#4) Application request limit reached", retry_after=900.0)
    scheduler = _scheduler(db, adapter, poll_interval=0.8)

    await scheduler._poll_all()

    assert adapter.fetch_calls == 1  # only the first account made a call
    assert scheduler._health_tracker.is_rate_limited() is True


async def test_a_platform_that_didnt_ask_for_spreading_is_polled_all_at_once() -> None:
    db = FakeDatabase()
    for index in range(3):
        _seed_account(db, f"acc-{index}")
    adapter = FakeAdapter()

    await _scheduler(db, adapter, poll_interval=60.0)._poll_all()

    assert adapter.fetch_calls == 3


async def test_the_adapter_is_asked_to_save_its_state_each_cycle() -> None:
    saved: List[bool] = []

    class Saving(FakeAdapter):
        async def persist_state(self) -> None:
            saved.append(True)

    class Broken(FakeAdapter):
        async def persist_state(self) -> None:
            raise RuntimeError("database down")

    await _scheduler(FakeDatabase(), Saving(), poll_interval=60.0)._persist_state()
    await _scheduler(FakeDatabase(), Broken(), poll_interval=60.0)._persist_state()  # must not raise

    assert saved == [True]


# ── rate limiter ────────────────────────────────────────────────────────


async def test_usage_recorded_elsewhere_counts_against_the_window() -> None:
    limiter = RateLimiter(max_requests=10, period_seconds=3600)
    await limiter.try_acquire()

    limiter.add_usage(6)

    assert limiter.used() == 7 and limiter.remaining() == 3
    limiter.add_usage(0)
    limiter.add_usage(-3)
    assert limiter.used() == 7


# ── Instagram client: pauses ────────────────────────────────────────────


def _ig_client(handler: Any, clock: Any = None) -> InstagramClient:
    kwargs: Dict[str, Any] = {"transport": httpx.MockTransport(handler)}
    if clock is not None:
        kwargs["clock"] = clock
    return InstagramClient("tok", "17841400", **kwargs)


def _ig_error(code: int, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(400, json={"error": {"message": "limit", "code": code}}, headers=headers or {})


async def test_repeated_rate_limits_pause_for_longer_each_time_until_a_call_succeeds() -> None:
    answers: List[httpx.Response] = []
    client = _ig_client(lambda request: answers.pop(0))

    waits = []
    for _ in range(4):
        answers.append(_ig_error(4))
        with pytest.raises(RateLimitedError) as error:
            await client.get_business_discovery("someone")
        waits.append(error.value.retry_after)
    assert waits == [900.0, 1800.0, 3600.0, 3600.0]  # 15 min, 30, then 1 h at most

    answers.append(httpx.Response(200, json={"business_discovery": {"id": "1", "media": {"data": []}}}))
    await client.get_business_discovery("someone")
    answers.append(_ig_error(4))
    with pytest.raises(RateLimitedError) as error:
        await client.get_business_discovery("someone")
    assert error.value.retry_after == 900.0  # a success starts the count over


async def test_metas_own_time_to_regain_access_is_used_when_it_is_sent() -> None:
    usage = json.dumps({"17841400": [{"type": "instagram", "call_count": 100, "estimated_time_to_regain_access": 20}]})
    client = _ig_client(lambda request: _ig_error(80002, {"x-business-use-case-usage": usage}))

    with pytest.raises(RateLimitedError) as error:
        await client.get_business_discovery("someone")

    assert error.value.retry_after == 20 * 60 + 60  # Meta's 20 minutes plus a minute's margin


async def test_a_zero_or_garbled_time_to_regain_access_is_ignored() -> None:
    for header in (
        json.dumps({"1": [{"estimated_time_to_regain_access": 0}]}),
        "not json",
        json.dumps({"1": "nope"}),
    ):
        client = _ig_client(lambda request, h=header: _ig_error(4, {"x-business-use-case-usage": h}))
        with pytest.raises(RateLimitedError) as error:
            await client.get_business_discovery("someone")
        assert error.value.retry_after == 900.0


async def test_the_usage_figure_knows_how_old_it_is() -> None:
    now = [100.0]
    client = _ig_client(
        lambda request: httpx.Response(
            200, json={"business_discovery": {"id": "1", "media": {"data": []}}}, headers={"x-app-usage": '{"call_count": 24}'}
        ),
        clock=lambda: now[0],
    )
    assert client.app_usage_age() is None  # nothing reported yet

    await client.get_business_discovery("someone")
    now[0] = 460.0

    assert client.app_usage_percent == 24 and client.app_usage_age() == 360.0


# ── Instagram adapter: follows Meta's usage figure ──────────────────────


class _UsageClient(FakeInstagramClient):
    """A fake whose usage figure has an age, like the real client's."""

    def __init__(self) -> None:
        super().__init__()
        self.age: Optional[float] = 5.0

    def app_usage_age(self) -> Optional[float]:
        return self.age


def _ig_adapter(client: _UsageClient, **kwargs: Any) -> InstagramAdapter:
    client.discovery = {**(client.discovery or {}), "media": [_media("1", _posted(datetime.timedelta(minutes=5)))]}
    return _make_adapter(client, hourly_call_limit=200, **kwargs)  # type: ignore[arg-type]


async def test_the_budget_catches_up_with_what_meta_says_was_used() -> None:
    """After a restart the budget is empty, but Meta's hour isn't."""
    client = _UsageClient()
    client.app_usage_percent = 60  # 120 of 200 calls already spent this hour
    adapter = _ig_adapter(client)

    await adapter.fetch_updates(_ACCOUNT, None)

    assert adapter._budget.used() == 121  # Meta's 120, plus this call
    assert adapter._budget.remaining() == 150 - 121


async def test_a_busy_hour_leaves_no_budget_for_a_check() -> None:
    client = _UsageClient()
    client.app_usage_percent = 80  # 160 > the 150 this bot allows itself
    adapter = _ig_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, "x|2026-01-01T00:00:00+0000")

    assert client.seen_usernames == [] and result.items == []


async def test_an_old_usage_figure_is_not_trusted_to_fill_the_budget() -> None:
    client = _UsageClient()
    client.app_usage_percent = 80
    client.age = 20 * 60.0  # twenty minutes old
    adapter = _ig_adapter(client)

    await adapter.fetch_updates(_ACCOUNT, None)

    assert client.seen_usernames == ["somecreator"] and adapter._budget.used() == 1


async def test_checks_stop_near_the_limit_and_resume_when_the_figure_is_old() -> None:
    client = _UsageClient()
    client.app_usage_percent = 96
    adapter = _ig_adapter(client)
    adapter._budget = RateLimiter(max_requests=10_000, period_seconds=3600)  # isolate the stop rule

    await adapter.fetch_updates(_ACCOUNT, None)
    assert client.seen_usernames == []  # fresh figure at 96% — stay away

    client.age = 16 * 60.0  # nothing was called, so it's never refreshed: probe once
    await adapter.fetch_updates(_ACCOUNT, None)
    assert client.seen_usernames == ["somecreator"]


async def test_the_slowdown_applies_only_to_a_recent_figure() -> None:
    client = _UsageClient()
    adapter = _make_adapter(client, hourly_call_limit=200)  # type: ignore[arg-type]
    base = adapter.poll_interval_seconds(5)

    client.app_usage_percent = 92
    assert adapter.poll_interval_seconds(5) == base * 4
    client.age = 2 * 3600.0
    assert adapter.poll_interval_seconds(5) == base


def test_instagram_asks_for_two_attempts_and_spread_out_polls() -> None:
    assert InstagramAdapter.max_retry_attempts == 2 and InstagramAdapter.spread_polls is True
    assert YouTubeAdapter.max_retry_attempts == 2 and YouTubeAdapter.spread_polls is True


_ACCOUNT = AccountRef(platform="instagram", platform_account_id="17841400", username="somecreator")


# ── Pacific time and the daily quota ────────────────────────────────────


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_UTC)


def test_pacific_time_follows_us_daylight_saving() -> None:
    assert pacific_offset(_at(2026, 1, 15)) == datetime.timedelta(hours=-8)
    assert pacific_offset(_at(2026, 7, 15)) == datetime.timedelta(hours=-7)
    # 2026: summer time starts Sunday March 8 at 2:00 local (10:00 UTC) and ends Sunday November 1 at 2:00 local (09:00 UTC)
    assert pacific_offset(_at(2026, 3, 8, 9, 59)) == datetime.timedelta(hours=-8)
    assert pacific_offset(_at(2026, 3, 8, 10, 0)) == datetime.timedelta(hours=-7)
    assert pacific_offset(_at(2026, 11, 1, 8, 59)) == datetime.timedelta(hours=-7)
    assert pacific_offset(_at(2026, 11, 1, 9, 0)) == datetime.timedelta(hours=-8)


def test_the_pacific_date_changes_at_pacific_midnight_not_utc_midnight() -> None:
    assert pacific_date(_at(2026, 10, 23, 6, 59)) == datetime.date(2026, 10, 22)  # 23:59 PDT the day before
    assert pacific_date(_at(2026, 10, 23, 7, 0)) == datetime.date(2026, 10, 23)
    assert pacific_date(_at(2026, 12, 23, 7, 59)) == datetime.date(2026, 12, 22)  # winter: UTC-8
    assert pacific_date(_at(2026, 12, 23, 8, 0)) == datetime.date(2026, 12, 23)


def test_seconds_until_the_quota_resets() -> None:
    assert seconds_until_pacific_midnight(_at(2026, 10, 23, 6, 0)) == 3600  # 23:00 PDT
    assert seconds_until_pacific_midnight(_at(2026, 10, 23, 7, 0)) == 24 * 3600
    assert seconds_until_pacific_midnight(_at(2026, 12, 23, 7, 30)) == 30 * 60  # 23:30 PST


class _Now:
    def __init__(self, moment: datetime.datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime.datetime:
        return self.moment


async def test_the_daily_quota_spends_until_empty_then_refuses_without_spending() -> None:
    quota = DailyQuota(10, clock=_Now(_at(2026, 10, 23, 12)))

    assert await quota.try_acquire(cost=6) is True
    assert await quota.try_acquire(cost=5) is False  # would go over
    assert quota.remaining() == 4
    assert await quota.try_acquire(cost=4) is True and quota.remaining() == 0


async def test_the_daily_quota_starts_over_at_pacific_midnight() -> None:
    now = _Now(_at(2026, 10, 23, 6, 59))
    quota = DailyQuota(10, clock=now)
    await quota.try_acquire(cost=10)
    assert quota.remaining() == 0

    now.moment = _at(2026, 10, 23, 7, 0)

    assert quota.remaining() == 10


async def test_the_daily_quota_can_be_saved_and_reloaded_the_same_day_only() -> None:
    now = _Now(_at(2026, 10, 23, 12))
    first = DailyQuota(100, clock=now)
    assert first.take_state() is None  # nothing to save yet
    await first.try_acquire(cost=40)
    state = first.take_state()
    assert state == {"day": "2026-10-23", "used": 40}
    assert first.take_state() is None  # saved: nothing new

    same_day = DailyQuota(100, clock=now)
    same_day.restore(state)
    assert same_day.remaining() == 60

    next_day = DailyQuota(100, clock=_Now(_at(2026, 10, 24, 12)))
    next_day.restore(state)
    assert next_day.remaining() == 100

    for junk in (None, {}, {"day": "2026-10-23", "used": "lots"}, {"day": "2026-10-23", "used": True}, "x"):
        fresh = DailyQuota(100, clock=now)
        fresh.restore(junk)  # type: ignore[arg-type]
        assert fresh.remaining() == 100


async def test_restoring_never_lowers_what_was_already_counted_and_caps_at_the_limit() -> None:
    now = _Now(_at(2026, 10, 23, 12))
    quota = DailyQuota(100, clock=now)
    await quota.try_acquire(cost=70)

    quota.restore({"day": "2026-10-23", "used": 20})
    assert quota.remaining() == 30
    quota.restore({"day": "2026-10-23", "used": 5000})
    assert quota.remaining() == 0


async def test_exhausting_the_quota_is_saved_too() -> None:
    quota = DailyQuota(100, clock=_Now(_at(2026, 10, 23, 12)))

    quota.exhaust()

    assert quota.remaining() == 0 and quota.take_state() == {"day": "2026-10-23", "used": 100}


# ── YouTube: the saved count and the daily reset ────────────────────────


class _FakeStateStore:
    def __init__(self, saved: Optional[Dict[str, Any]] = None) -> None:
        self.values: Dict[str, Dict[str, Any]] = {"youtube_quota": saved} if saved else {}
        self.fail = False

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.fail:
            raise RuntimeError("database down")
        return self.values.get(key)

    async def set(self, key: str, value: Dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("database down")
        self.values[key] = value


def _yt_adapter(store: Any, now: _Now) -> YouTubeAdapter:
    return YouTubeAdapter(object(), daily_quota_units=100, wall_clock=now, state_store=store)  # type: ignore[arg-type]


async def test_youtube_remembers_todays_spending_across_a_restart() -> None:
    now = _Now(_at(2026, 10, 23, 12))
    store = _FakeStateStore()
    before = _yt_adapter(store, now)
    await before._budget.try_acquire(cost=35)
    await before.persist_state()
    assert store.values["youtube_quota"] == {"day": "2026-10-23", "used": 35}

    after = _yt_adapter(store, now)  # the bot restarted
    assert after.quota_snapshot() == (100, 100)
    await after.restore_state()

    assert after.quota_snapshot() == (65, 100)


async def test_youtube_only_saves_when_something_changed() -> None:
    store = _FakeStateStore()
    adapter = _yt_adapter(store, _Now(_at(2026, 10, 23, 12)))

    await adapter.persist_state()
    assert store.values == {}

    await adapter._budget.try_acquire(cost=1)
    await adapter.persist_state()
    store.values.clear()
    await adapter.persist_state()
    assert store.values == {}  # already saved


async def test_a_failed_save_is_offered_again_next_cycle() -> None:
    store = _FakeStateStore()
    adapter = _yt_adapter(store, _Now(_at(2026, 10, 23, 12)))
    await adapter._budget.try_acquire(cost=9)

    store.fail = True
    with pytest.raises(RuntimeError):
        await adapter.persist_state()
    store.fail = False
    await adapter.persist_state()

    assert store.values["youtube_quota"]["used"] == 9


async def test_a_failure_reloading_the_count_just_starts_from_zero() -> None:
    store = _FakeStateStore({"day": "2026-10-23", "used": 50})
    store.fail = True
    adapter = _yt_adapter(store, _Now(_at(2026, 10, 23, 12)))

    await adapter.restore_state()  # must not raise

    assert adapter.quota_snapshot() == (100, 100)


async def test_youtubes_quota_exceeded_empties_the_local_count_too() -> None:
    class Exhausted:
        async def get_channel(self, **_: Any) -> Dict[str, Any]:
            return {"id": "UCabc", "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}}, "snippet": {}}

        async def list_uploads(self, *_: Any, **__: Any) -> Any:
            raise QuotaExceededError("daily quota exceeded", retry_after=3600.0)

        async def aclose(self) -> None:
            pass

    adapter = YouTubeAdapter(Exhausted(), daily_quota_units=10_000, wall_clock=_Now(_at(2026, 10, 23, 12)))  # type: ignore[arg-type]

    with pytest.raises(QuotaExceededError):
        await adapter.fetch_updates(AccountRef(platform="youtube", platform_account_id="UCabc", username="x"), None)

    assert adapter.quota_snapshot() == (0, 10_000)


async def test_youtubes_wait_after_quota_exceeded_ends_at_the_pacific_reset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "Daily Limit Exceeded"}})

    client = YouTubeClient("key", transport=httpx.MockTransport(handler), clock=_Now(_at(2026, 10, 23, 6, 0)))

    with pytest.raises(QuotaExceededError) as error:
        await client.get_channel(channel_id="UCabc")

    assert isinstance(error.value, RateLimitedError)
    assert error.value.retry_after == 3600 + 60  # until 00:00 Pacific, plus a minute
