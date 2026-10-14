"""TwitterAdapter: adding an account, the baseline, new posts only, one
fetch per account shared by every server, the interval that keeps X's
30-per-15-minutes limit, and renamed handles."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from events.models import EventType
from platforms.base import AccountRef
from platforms.twitter.adapter import TwitterAdapter
from tests.unit.twitter_fixtures import post, retweet
from utils.errors import PermanentPlatformError, RateLimitedError

_ACCOUNT = AccountRef(platform="twitter", platform_account_id="11348282", username="NASA")


class FakeClient:
    """Timelines keyed by lower-case @handle; by-ID lookups find the same
    timeline through its owner's ID."""

    def __init__(self) -> None:
        self.timelines: Dict[str, List[Dict[str, Any]]] = {}
        self.fetched: List[str] = []
        self.remaining: Dict[str, Optional[int]] = {}
        self.reset_in = 0.0

    async def get_timeline_by_handle(self, handle: str) -> List[Dict[str, Any]]:
        self.fetched.append(f"@{handle}")
        return self.timelines.get(handle.lower(), [])

    async def get_timeline_by_id(self, user_id: str) -> List[Dict[str, Any]]:
        self.fetched.append(user_id)
        for posts in self.timelines.values():
            if posts and posts[0]["user"]["id_str"] == user_id:
                return posts
        return []

    def requests_remaining(self, kind: str) -> Optional[int]:
        return self.remaining.get(kind)

    def seconds_until_reset(self, kind: str) -> float:
        return max(self.reset_in, 1.0)

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _adapter(client: FakeClient, clock: Optional[Clock] = None) -> TwitterAdapter:
    return TwitterAdapter(client, clock=clock or Clock())  # type: ignore[arg-type]


async def test_adding_an_account_resolves_the_handle_from_its_posts() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("2")]

    ref = await _adapter(client).validate_account("https://x.com/NASA")

    assert ref == _ACCOUNT


async def test_an_account_without_public_posts_cant_be_added() -> None:
    with pytest.raises(PermanentPlatformError):
        await _adapter(FakeClient()).validate_account("@nobody")


async def test_the_first_check_is_a_baseline_without_alerts() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30"), retweet("40"), post("20")]

    result = await _adapter(client).fetch_updates(_ACCOUNT, None)

    assert result.items == [] and result.next_cursor == "40"


async def test_an_account_that_never_posted_gets_its_first_post_alerted() -> None:
    client = FakeClient()
    clock = Clock()
    adapter = _adapter(client, clock)
    baseline = await adapter.fetch_updates(_ACCOUNT, None)
    client.timelines["nasa"] = [post("5")]
    clock.now += 3600

    result = await adapter.fetch_updates(_ACCOUNT, baseline.next_cursor)

    assert baseline.next_cursor == "0"
    assert [p["id_str"] for p in result.items] == ["5"]


async def test_only_new_original_posts_are_alerted_oldest_first() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("60"), retweet("55"), post("50", in_reply_to_status_id_str="1"), post("45"), post("30")]
    adapter = _adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, "40")

    assert [p["id_str"] for p in result.items] == ["45", "60"]
    assert result.next_cursor == "60"
    event = adapter.normalize_event(_ACCOUNT, result.items[0])
    assert event.event_type == EventType.POST_CREATED and event.url == "https://x.com/NASA/status/45"


async def test_nothing_new_keeps_the_cursor() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30")]

    result = await _adapter(client).fetch_updates(_ACCOUNT, "30")

    assert result.items == [] and result.next_cursor == "30"


async def test_one_fetch_is_shared_by_every_server_following_the_account() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30")]
    clock = Clock()
    adapter = _adapter(client, clock)
    adapter.poll_interval_seconds(1)

    for _server in range(3):
        await adapter.fetch_updates(_ACCOUNT, "10")
    clock.now += 61
    await adapter.fetch_updates(_ACCOUNT, "30")

    assert client.fetched == ["11348282", "11348282"]  # by user ID, not @handle
    assert adapter.take_request_count() == 2


@pytest.mark.parametrize(("accounts", "interval"), [(0, 60.0), (1, 60.0), (10, 450.0), (50, 2250.0)])
def test_interval_keeps_under_30_requests_per_15_minutes(accounts: int, interval: float) -> None:
    assert _adapter(FakeClient()).poll_interval_seconds(accounts) == interval


async def test_checks_pause_when_their_window_is_used_up_but_adding_has_its_own() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30")]
    client.remaining = {"id": 1, "handle": 25}
    client.reset_in = 400

    with pytest.raises(RateLimitedError) as exc_info:
        await _adapter(client).fetch_updates(_ACCOUNT, "10")
    ref = await _adapter(client).validate_account("NASA")

    assert exc_info.value.retry_after == 400
    assert "try again in about 7 minutes" in str(exc_info.value)
    assert ref == _ACCOUNT


async def test_adding_says_when_to_try_again_when_x_is_busy() -> None:
    client = FakeClient()
    client.remaining = {"handle": 0}
    client.reset_in = 60

    with pytest.raises(RateLimitedError, match="try again in about 1 minute$"):
        await _adapter(client).validate_account("NASA")


async def test_an_account_that_changes_its_handle_keeps_working() -> None:
    client = FakeClient()
    client.timelines["nasa_new"] = [post("30", user={**post("0")["user"], "screen_name": "NASA_new"})]

    result = await _adapter(client).fetch_updates(_ACCOUNT, "10")

    assert [p["id_str"] for p in result.items] == ["30"]
    assert _adapter(client).normalize_event(_ACCOUNT, result.items[0]).url == "https://x.com/NASA_new/status/30"


async def test_get_live_status_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter(FakeClient()).get_live_status(_ACCOUNT)


def test_the_real_clock_is_monotonic() -> None:
    assert TwitterAdapter(FakeClient())._clock is time.monotonic  # type: ignore[arg-type]
