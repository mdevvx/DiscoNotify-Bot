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
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

_ACCOUNT = AccountRef(platform="twitter", platform_account_id="11348282", username="NASA")


class FakeClient:
    """Timelines keyed by lower-case @handle; by-ID lookups find the same
    timeline through its owner's ID."""

    def __init__(self) -> None:
        self.timelines: Dict[str, List[Dict[str, Any]]] = {}
        self.fetched: List[str] = []
        self.remaining: Dict[str, Optional[int]] = {}
        self.reset_in = 0.0
        self.lookup_down = False
        self.protected: set = set()
        self.fx_posts: Dict[str, List[Dict[str, Any]]] = {}
        self.users: Dict[str, Dict[str, Any]] = {}

    async def get_recent_posts_fx(self, handle: str) -> List[Dict[str, Any]]:
        self.fetched.append(f"fx:{handle}")
        return self.fx_posts.get(handle.lower(), [])

    async def lookup_user(self, handle: str) -> Optional[Dict[str, Any]]:
        """FxTwitter: finds the account from its timeline's owner."""
        if self.lookup_down:
            raise TransientPlatformError("FxTwitter lookup error 503")
        posts = self.timelines.get(handle.lower())
        if handle.lower() in self.protected:
            return {"id": "77", "screen_name": handle, "protected": True}
        if handle.lower() in self.users:
            return self.users[handle.lower()]
        if not posts:
            return None
        user = posts[0]["user"]
        return {"id": user["id_str"], "screen_name": user["screen_name"], "protected": False}

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


async def test_adding_an_account_looks_it_up_then_reads_it_by_id() -> None:
    """Never touches X's by-@handle window, which other apps on a shared
    host IP (Railway) kept using up (2026-10-15)."""
    client = FakeClient()
    client.timelines["nasa"] = [post("2")]
    client.remaining = {"handle": 0}

    ref = await _adapter(client).validate_account("https://x.com/NASA")

    assert ref == _ACCOUNT
    assert client.fetched == ["11348282"]


async def test_an_unknown_account_cant_be_added() -> None:
    with pytest.raises(PermanentPlatformError, match="no X account @nobody"):
        await _adapter(FakeClient()).validate_account("@nobody")


async def test_a_private_account_cant_be_added() -> None:
    client = FakeClient()
    client.protected.add("secret")

    with pytest.raises(PermanentPlatformError, match="private"):
        await _adapter(client).validate_account("@secret")


async def test_if_the_lookup_service_is_down_x_is_asked_by_handle() -> None:
    client = FakeClient()
    client.timelines["nasa"] = [post("2")]
    client.lookup_down = True

    ref = await _adapter(client).validate_account("NASA")

    assert ref == _ACCOUNT and client.fetched == ["@NASA"]


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


async def test_checks_pause_when_their_window_is_nearly_used_up_but_adding_still_works() -> None:
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
    client.lookup_down = True
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


_COOKED = {"id_str": "2008104784159805440", "screen_name": "Cookedcom", "name": "Cooked"}
_COOKED_REF = AccountRef(platform="twitter", platform_account_id="2008104784159805440", username="Cookedcom")


def _cooked_client() -> FakeClient:
    """X's embedded timeline is empty for @Cookedcom although it posts
    (real case 2026-10-15); FxTwitter has its posts."""
    client = FakeClient()
    client.users["cookedcom"] = {"id": _COOKED["id_str"], "screen_name": "Cookedcom", "protected": False}
    client.fx_posts["cookedcom"] = [post("200", user=_COOKED), post("190", user=_COOKED)]
    return client


async def test_an_account_with_an_empty_x_timeline_is_added_from_fxtwitter() -> None:
    client = _cooked_client()

    ref = await _adapter(client).validate_account("@Cookedcom")

    assert ref == _COOKED_REF
    assert client.fetched == ["2008104784159805440", "fx:Cookedcom"]


async def test_such_an_account_is_then_checked_on_fxtwitter_only() -> None:
    client = _cooked_client()
    clock = Clock()
    adapter = _adapter(client, clock)
    await adapter.validate_account("@Cookedcom")
    client.fx_posts["cookedcom"].insert(0, post("210", user=_COOKED))
    client.fetched.clear()
    clock.now += 3600

    result = await adapter.fetch_updates(_COOKED_REF, "200")

    assert [p["id_str"] for p in result.items] == ["210"]
    assert client.fetched == ["fx:Cookedcom"]  # X's empty timeline isn't asked again


async def test_fxtwitter_posts_by_another_account_are_ignored() -> None:
    """FxTwitter goes by @handle; if the handle now belongs to someone else,
    their posts must not be alerted as this account's."""
    client = _cooked_client()
    client.fx_posts["cookedcom"] = [post("300", user={"id_str": "1", "screen_name": "Cookedcom"})]
    adapter = _adapter(client)
    adapter._embed_empty.add(_COOKED_REF.platform_account_id)

    result = await adapter.fetch_updates(_COOKED_REF, "200")

    assert result.items == []
