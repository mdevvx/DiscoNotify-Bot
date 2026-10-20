"""TikTokAdapter: adding an account, the baseline, new videos only, one fetch
shared by every server, the interval, and how a missing account is judged."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from events.models import EventType
from platforms.base import AccountRef, event_types_for_capabilities
from platforms.tiktok.adapter import TikTokAdapter
from tests.unit.tiktok_fixtures import user, video, video_id
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

_ACCOUNT = AccountRef(platform="tiktok", platform_account_id="107955", username="nasa")


class FakeClient:
    def __init__(self) -> None:
        self.profiles: Dict[str, Optional[Dict[str, Any]]] = {}
        self.fetched: List[str] = []
        self.error: Optional[Exception] = None

    def set(self, handle: str, videos: List[Dict[str, Any]], **user_args: Any) -> None:
        self.profiles[handle.lower()] = {"user": user(handle=handle, **user_args), "videos": videos}

    async def get_profile(self, handle: str) -> Optional[Dict[str, Any]]:
        self.fetched.append(handle)
        if self.error is not None:
            raise self.error
        return self.profiles.get(handle.lower())

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _adapter(client: FakeClient, clock: Optional[Clock] = None) -> TikTokAdapter:
    return TikTokAdapter(client, clock=clock or Clock())  # type: ignore[arg-type]


def _expect_retryable(caught: pytest.ExceptionInfo) -> None:
    assert not isinstance(caught.value, PermanentPlatformError)


async def test_adding_an_account_by_link() -> None:
    client = FakeClient()
    client.set("nasa", [video(2)])

    ref = await _adapter(client).validate_account("https://www.tiktok.com/@NASA")

    assert ref == _ACCOUNT and client.fetched == ["NASA"]


async def test_an_account_with_no_videos_can_be_added() -> None:
    client = FakeClient()
    client.set("nasa", [])

    assert await _adapter(client).validate_account("@nasa") == _ACCOUNT


async def test_an_unknown_account_cant_be_added() -> None:
    with pytest.raises(PermanentPlatformError, match="no TikTok account @nobody"):
        await _adapter(FakeClient()).validate_account("@nobody")


async def test_a_private_account_cant_be_added() -> None:
    client = FakeClient()
    client.set("secret", [], private=True)

    with pytest.raises(PermanentPlatformError, match="private"):
        await _adapter(client).validate_account("@secret")


async def test_a_bad_username_never_reaches_tiktok() -> None:
    client = FakeClient()

    with pytest.raises(PermanentPlatformError):
        await _adapter(client).validate_account("not a name!")

    assert client.fetched == []


async def test_when_tiktok_is_blocking_or_down_adding_fails_with_a_retryable_error() -> None:
    client = FakeClient()
    client.error = RateLimitedError("blocked", retry_after=600)
    with pytest.raises(RateLimitedError):
        await _adapter(client).validate_account("@nasa")
    assert client.fetched == ["nasa"]  # a block is not asked again

    client = FakeClient()
    client.error = TransientPlatformError("timed out")
    with pytest.raises(TransientPlatformError):
        await _adapter(client).validate_account("@nasa")
    assert client.fetched == ["nasa", "nasa"]  # a timeout gets one more try


async def test_a_timeout_on_the_first_try_is_retried_and_adding_succeeds() -> None:
    client = FakeClient()
    client.set("nasa", [video(1)])
    real = client.get_profile
    calls = []

    async def flaky(handle: str):
        calls.append(handle)
        if len(calls) == 1:
            raise TransientPlatformError("TikTok request timed out: @nasa")
        return await real(handle)

    client.get_profile = flaky  # type: ignore[method-assign]

    assert await _adapter(client).validate_account("@nasa") == _ACCOUNT
    assert len(calls) == 2


async def test_the_first_check_is_a_baseline_without_alerts() -> None:
    client = FakeClient()
    client.set("nasa", [video(3), video(2)])

    result = await _adapter(client).fetch_updates(_ACCOUNT, None)

    assert result.items == [] and result.next_cursor == video_id(3)


async def test_an_account_that_never_posted_gets_its_first_video_alerted() -> None:
    client = FakeClient()
    client.set("nasa", [])
    adapter = _adapter(client)

    baseline = await adapter.fetch_updates(_ACCOUNT, None)
    assert baseline.next_cursor == "0"

    client.set("nasa", [video(1)])
    adapter._snapshots.clear()
    result = await adapter.fetch_updates(_ACCOUNT, baseline.next_cursor)

    assert [item["video"]["id"] for item in result.items] == [video_id(1)]


async def test_only_new_public_videos_are_alerted_oldest_first() -> None:
    client = FakeClient()
    client.set("nasa", [video(5), video(4, private=True), video(3), video(2)])

    result = await _adapter(client).fetch_updates(_ACCOUNT, video_id(2))

    assert [item["video"]["id"] for item in result.items] == [video_id(3), video_id(5)]
    assert result.next_cursor == video_id(5)
    assert result.items[0]["user"]["uniqueId"] == "nasa"


async def test_a_pinned_old_video_listed_first_is_not_news_and_does_not_move_the_cursor_back() -> None:
    client = FakeClient()
    client.set("nasa", [video(1), video(9), video(8)])

    result = await _adapter(client).fetch_updates(_ACCOUNT, video_id(9))

    assert result.items == [] and result.next_cursor == video_id(9)


async def test_a_deleted_newest_video_keeps_the_cursor() -> None:
    client = FakeClient()
    client.set("nasa", [video(3)])

    result = await _adapter(client).fetch_updates(_ACCOUNT, video_id(7))

    assert result.items == [] and result.next_cursor == video_id(7)


async def test_one_fetch_is_shared_by_every_server_following_the_account() -> None:
    client = FakeClient()
    client.set("nasa", [video(2)])
    adapter = _adapter(client)

    await adapter.fetch_updates(_ACCOUNT, None)
    await adapter.fetch_updates(_ACCOUNT, None)

    assert client.fetched == ["nasa"]


async def test_the_snapshot_expires_after_half_an_interval() -> None:
    client = FakeClient()
    client.set("nasa", [video(2)])
    clock = Clock()
    adapter = _adapter(client, clock)
    adapter.poll_interval_seconds(1)

    await adapter.fetch_updates(_ACCOUNT, None)
    clock.now += 59
    await adapter.fetch_updates(_ACCOUNT, None)
    assert client.fetched == ["nasa"]  # 59s < 120s / 2

    clock.now += 2
    await adapter.fetch_updates(_ACCOUNT, None)
    assert client.fetched == ["nasa", "nasa"]


@pytest.mark.parametrize(("accounts", "interval"), [(0, 120), (1, 120), (30, 120), (100, 400), (300, 1200)])
def test_the_interval_stretches_with_the_number_of_accounts(accounts: int, interval: float) -> None:
    assert _adapter(FakeClient()).poll_interval_seconds(accounts) == interval


async def test_a_renamed_account_is_not_confused_with_whoever_took_the_old_name() -> None:
    client = FakeClient()
    client.set("nasa", [video(2)], uid="999")  # someone else now has @nasa

    with pytest.raises(TransientPlatformError) as caught:
        await _adapter(client).fetch_updates(_ACCOUNT, video_id(1))

    _expect_retryable(caught)


async def test_one_missing_answer_does_not_switch_the_account_off() -> None:
    with pytest.raises(TransientPlatformError) as caught:
        await _adapter(FakeClient()).fetch_updates(_ACCOUNT, video_id(1))  # TikTok doesn't know @nasa right now

    _expect_retryable(caught)


async def test_retries_seconds_apart_count_as_one_miss() -> None:
    clock = Clock()
    adapter = _adapter(FakeClient(), clock)

    for _ in range(10):
        clock.now += 5
        adapter._snapshots.clear()
        with pytest.raises(TransientPlatformError) as caught:
            await adapter.fetch_updates(_ACCOUNT, video_id(1))
        _expect_retryable(caught)


async def test_missing_in_three_separate_checks_marks_it_gone() -> None:
    clock = Clock()
    adapter = _adapter(FakeClient(), clock)

    for _ in range(2):
        with pytest.raises(TransientPlatformError) as caught:
            await adapter.fetch_updates(_ACCOUNT, video_id(1))
        _expect_retryable(caught)
        clock.now += 400
        adapter._snapshots.clear()

    with pytest.raises(PermanentPlatformError, match="can't be found any more"):
        await adapter.fetch_updates(_ACCOUNT, video_id(1))


async def test_coming_back_resets_the_misses() -> None:
    client = FakeClient()
    clock = Clock()
    adapter = _adapter(client, clock)

    for _ in range(2):
        with pytest.raises(TransientPlatformError):
            await adapter.fetch_updates(_ACCOUNT, video_id(1))
        clock.now += 400
        adapter._snapshots.clear()

    client.set("nasa", [video(1)])
    await adapter.fetch_updates(_ACCOUNT, video_id(1))
    client.profiles.clear()
    clock.now += 400
    adapter._snapshots.clear()

    with pytest.raises(TransientPlatformError) as caught:
        await adapter.fetch_updates(_ACCOUNT, video_id(1))
    _expect_retryable(caught)


async def test_an_account_that_turned_private_is_judged_like_a_missing_one() -> None:
    client = FakeClient()
    client.set("nasa", [], private=True)

    with pytest.raises(TransientPlatformError) as caught:
        await _adapter(client).fetch_updates(_ACCOUNT, video_id(1))

    _expect_retryable(caught)


async def test_blocks_and_timeouts_pass_through_for_the_worker_to_handle() -> None:
    client = FakeClient()
    client.error = RateLimitedError("blocked", retry_after=600)
    with pytest.raises(RateLimitedError):
        await _adapter(client).fetch_updates(_ACCOUNT, None)

    client.error = TransientPlatformError("timed out")
    with pytest.raises(TransientPlatformError):
        await _adapter(client).fetch_updates(_ACCOUNT, None)


async def test_get_live_status_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter(FakeClient()).get_live_status(_ACCOUNT)


def test_capabilities_are_videos_only() -> None:
    assert event_types_for_capabilities(TikTokAdapter.capabilities) == frozenset({EventType.VIDEO_PUBLISHED})
