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
    def __init__(self) -> None:
        self.timelines: Dict[str, List[Dict[str, Any]]] = {}
        self.fetched: List[str] = []
        self.requests_remaining: Optional[int] = None
        self.reset_in = 0.0

    async def get_timeline(self, handle: str) -> List[Dict[str, Any]]:
        self.fetched.append(handle)
        return self.timelines.get(handle.lower(), [])

    def seconds_until_reset(self) -> float:
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

    assert client.fetched == ["NASA", "NASA"]
    assert adapter.take_request_count() == 2


@pytest.mark.parametrize(("accounts", "interval"), [(0, 60.0), (1, 60.0), (10, 450.0), (50, 2250.0)])
def test_interval_keeps_under_30_requests_per_15_minutes(accounts: int, interval: float) -> None:
    assert _adapter(FakeClient()).poll_interval_seconds(accounts) == interval


async def test_checks_pause_when_the_window_is_nearly_used_up_but_adding_still_works() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30")]
    client.requests_remaining = 2
    client.reset_in = 400

    with pytest.raises(RateLimitedError) as exc_info:
        await _adapter(client).fetch_updates(_ACCOUNT, "10")
    ref = await _adapter(client).validate_account("NASA")  # the reserved requests

    assert exc_info.value.retry_after == 400
    assert ref == _ACCOUNT


async def test_a_handle_now_owned_by_another_account_is_reported() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("30", user={"id_str": "999", "screen_name": "NASA"})]

    with pytest.raises(PermanentPlatformError, match="renamed"):
        await _adapter(client).fetch_updates(_ACCOUNT, "10")


async def test_get_live_status_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter(FakeClient()).get_live_status(_ACCOUNT)


def test_the_real_clock_is_monotonic() -> None:
    assert TwitterAdapter(FakeClient())._clock is time.monotonic  # type: ignore[arg-type]
