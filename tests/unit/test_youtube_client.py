"""YouTubeClient's error classification (status code/reason -> the right
PlatformError subclass), driven through httpx.MockTransport so no real
network call is ever made."""

from __future__ import annotations

import json

import httpx
import pytest

from platforms.youtube.client import YouTubeClient
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError


def _client_with(handler) -> YouTubeClient:
    return YouTubeClient("fake-api-key", transport=httpx.MockTransport(handler))


def _json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body))


async def test_successful_response_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"items": [{"id": "UCabc"}]})

    client = _client_with(handler)
    channel = await client.get_channel(channel_id="UCabc")

    assert channel == {"id": "UCabc"}


async def test_channel_not_found_returns_none_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"items": []})

    client = _client_with(handler)
    channel = await client.get_channel(channel_id="UCmissing")

    assert channel is None


async def test_rate_limit_exceeded_raises_rate_limited_error_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "30"},
            content=json.dumps({"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "slow down"}}),
        )

    client = _client_with(handler)
    with pytest.raises(RateLimitedError) as exc_info:
        await client.get_channel(channel_id="UCabc")

    assert exc_info.value.retry_after == 30.0


async def test_quota_exceeded_raises_rate_limited_error_with_a_long_default_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "Daily Limit Exceeded"}}
        )

    client = _client_with(handler)
    with pytest.raises(RateLimitedError) as exc_info:
        await client.get_channel(channel_id="UCabc")

    assert exc_info.value.retry_after is not None
    assert exc_info.value.retry_after > 3600  # several hours, not a short retry


async def test_video_not_found_raises_permanent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"error": {"errors": [{"reason": "videoNotFound"}], "message": "not found"}})

    client = _client_with(handler)
    with pytest.raises(PermanentPlatformError):
        await client.get_videos(["missing"])


async def test_bad_request_raises_permanent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"error": {"message": "invalid parameter"}})

    client = _client_with(handler)
    with pytest.raises(PermanentPlatformError):
        await client.get_channel(channel_id="not-a-valid-id")


async def test_invalid_api_key_is_treated_as_transient_not_account_specific() -> None:
    """A 401/403 that isn't quotaExceeded is a config problem (bad key),
    not evidence that this specific channel is invalid — see the comment
    in client.py for why this must not be PermanentPlatformError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(401, {"error": {"message": "API key not valid"}})

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_channel(channel_id="UCabc")


async def test_server_error_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content="internal error")

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_channel(channel_id="UCabc")


async def test_timeout_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_channel(channel_id="UCabc")


async def test_get_videos_sends_comma_joined_ids() -> None:
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return _json_response(200, {"items": []})

    client = _client_with(handler)
    await client.get_videos(["a", "b", "c"])

    assert seen_params["id"] == "a,b,c"
    # player + maxHeight are what return the embed dimensions the Shorts
    # heuristic needs.
    assert "player" in seen_params["part"].split(",")
    assert seen_params["maxHeight"]


async def test_get_videos_with_no_ids_does_not_make_a_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _json_response(200, {"items": []})

    client = _client_with(handler)
    result = await client.get_videos([])

    assert result == []
    assert called is False
