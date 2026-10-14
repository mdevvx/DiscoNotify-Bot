"""TwitterClient: reading X's embedded timeline page and its rate-limit
headers, through httpx.MockTransport (no real network)."""

from __future__ import annotations

import time
from typing import List

import httpx
import pytest

from platforms.twitter.client import TwitterClient, require_posts
from tests.unit.twitter_fixtures import page, post, retweet
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError


def _client_with(handler) -> TwitterClient:
    return TwitterClient(transport=httpx.MockTransport(handler))


async def test_reads_the_posts_and_the_rate_limit_headers() -> None:
    seen: List[httpx.Request] = []
    reset_at = time.time() + 600

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"x-rate-limit-remaining": "23", "x-rate-limit-reset": str(reset_at)}
        return httpx.Response(200, text=page([post("2"), retweet("1")]), headers=headers)

    client = _client_with(handler)
    posts = await client.get_timeline_by_handle("NASA")
    await client.get_timeline_by_id("11348282")

    assert [p["id_str"] for p in posts] == ["2", "1"]
    assert seen[0].url.path == "/srv/timeline-profile/screen-name/NASA"
    assert seen[1].url.path == "/srv/timeline-profile/user-id/11348282"
    assert "Mozilla" in seen[0].headers["User-Agent"]
    assert client.requests_remaining("handle") == 23 and client.requests_remaining("id") == 23
    assert 590 < client.seconds_until_reset("id") <= 600


async def test_the_two_lookup_kinds_have_separate_limits() -> None:
    reset_at = time.time() + 600

    def handler(request: httpx.Request) -> httpx.Response:
        remaining = "0" if "screen-name" in request.url.path else "29"
        return httpx.Response(200, text=page([post("1")]), headers={"x-rate-limit-remaining": remaining, "x-rate-limit-reset": str(reset_at)})

    client = _client_with(handler)
    await client.get_timeline_by_handle("NASA")
    await client.get_timeline_by_id("1")

    assert client.requests_remaining("handle") == 0 and client.requests_remaining("id") == 29


async def test_429_waits_for_the_window_to_reset() -> None:
    reset_at = time.time() + 300
    client = _client_with(
        lambda r: httpx.Response(429, text="Rate limit exceeded", headers={"x-rate-limit-reset": str(reset_at), "x-rate-limit-remaining": "0"})
    )

    with pytest.raises(RateLimitedError, match="try again in about 5 minutes") as exc_info:
        await client.get_timeline_by_handle("NASA")

    assert 290 < exc_info.value.retry_after <= 300


async def test_an_unknown_account_has_no_posts_and_cant_be_added() -> None:
    client = _client_with(lambda r: httpx.Response(200, text=page([])))

    posts = await client.get_timeline_by_handle("nobody_here")

    assert posts == []
    with pytest.raises(PermanentPlatformError, match="Couldn't find public posts"):
        require_posts(posts, "nobody_here")


async def test_a_changed_page_is_a_temporary_error_not_a_bad_account() -> None:
    client = _client_with(lambda r: httpx.Response(200, text="<html>something else</html>"))

    with pytest.raises(TransientPlatformError, match="page format changed"):
        await client.get_timeline_by_id("11348282")


async def test_server_errors_and_timeouts_are_temporary() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientPlatformError):
        await _client_with(lambda r: httpx.Response(503)).get_timeline_by_id("11348282")
    with pytest.raises(TransientPlatformError):
        await _client_with(timeout).get_timeline_by_id("11348282")


def test_the_profile_comes_from_the_accounts_own_post_not_a_retweeted_one() -> None:
    other = post("9", user={"id_str": "1", "screen_name": "Someone"})

    assert require_posts([other, post("8")], "nasa")["id_str"] == "11348282"


async def test_lookup_finds_the_account_or_none_for_a_missing_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.fxtwitter.com"
        if request.url.path == "/NASA":
            return httpx.Response(200, json={"code": 200, "user": {"id": "11348282", "screen_name": "NASA", "protected": False}})
        return httpx.Response(302, headers={"location": "https://github.com/FxEmbed/FxEmbed"})

    client = _client_with(handler)

    assert (await client.lookup_user("NASA"))["id"] == "11348282"
    assert await client.lookup_user("nobody") is None


async def test_lookup_service_errors_are_temporary() -> None:
    with pytest.raises(TransientPlatformError):
        await _client_with(lambda r: httpx.Response(503)).lookup_user("NASA")
