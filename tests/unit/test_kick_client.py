"""KickClient's token handling and error classification (status code -> the
right PlatformError subclass), driven through httpx.MockTransport so no
real network call is ever made."""

from __future__ import annotations

import json

import httpx
import pytest

from platforms.kick.client import KickClient
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError


def _client_with(handler) -> KickClient:
    return KickClient("fake-client-id", "fake-client-secret", transport=httpx.MockTransport(handler))


def _json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body))


def _token_response(*, access_token: str = "test-token", expires_in: int = 3600) -> httpx.Response:
    return _json_response(200, {"access_token": access_token, "expires_in": expires_in, "token_type": "Bearer"})


def _is_auth_request(request: httpx.Request) -> bool:
    return request.url.host == "id.kick.com"


async def test_get_channel_by_slug_returns_first_matching_channel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(200, {"data": [{"broadcaster_user_id": 123, "slug": "somestreamer"}], "message": "Success"})

    client = _client_with(handler)
    channel = await client.get_channel_by_slug("somestreamer")

    assert channel == {"broadcaster_user_id": 123, "slug": "somestreamer"}


async def test_get_channel_by_slug_returns_none_when_no_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(200, {"data": [], "message": "Success"})

    client = _client_with(handler)
    channel = await client.get_channel_by_slug("nonexistent")

    assert channel is None


async def test_get_livestreams_batches_every_id_into_one_call() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        seen.append(request.url.params.get_list("broadcaster_user_id"))
        return _json_response(200, {"data": [{"broadcaster_user_id": 123}], "message": "OK"})

    client = _client_with(handler)
    streams = await client.get_livestreams(["123", "456"])

    assert streams == [{"broadcaster_user_id": 123}]
    assert seen == [["123", "456"]]


async def test_get_livestreams_with_no_ids_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await _client_with(handler).get_livestreams([]) == []


async def test_unknown_slug_returns_none() -> None:
    """Kick answers an unknown slug with HTTP 400 "Invalid request", not an
    empty list (verified against the live API 2026-10-12)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(400, {"data": {}, "message": "Invalid request"})

    assert await _client_with(handler).get_channel_by_slug("no-such-channel") is None


async def test_token_is_fetched_once_and_reused_across_calls() -> None:
    auth_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if _is_auth_request(request):
            auth_calls += 1
            return _token_response()
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    await client.get_livestreams(["1"])
    await client.get_livestreams(["2"])
    await client.get_livestreams(["3"])

    assert auth_calls == 1


async def test_token_sends_client_credentials_grant() -> None:
    seen_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return _token_response()
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    await client.get_livestreams(["1"])

    assert seen_body["client_id"] == "fake-client-id"
    assert seen_body["client_secret"] == "fake-client-secret"
    assert seen_body["grant_type"] == "client_credentials"


async def test_helix_request_sends_bearer_token_header() -> None:
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response(access_token="abc123")
        seen_headers.update(dict(request.headers))
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    await client.get_livestreams(["1"])

    assert seen_headers["authorization"] == "Bearer abc123"


async def test_token_is_refreshed_once_it_expires() -> None:
    auth_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if _is_auth_request(request):
            auth_calls += 1
            # expires_in=0 (clamped) means the token is treated as already
            # due for refresh by the very next call, without needing to
            # fake the passage of time.
            return _token_response(expires_in=0)
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    await client.get_livestreams(["1"])
    await client.get_livestreams(["2"])

    assert auth_calls == 2


async def test_unexpected_401_forces_a_token_refresh_and_retries_once() -> None:
    auth_calls = 0
    channel_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, channel_calls
        if _is_auth_request(request):
            auth_calls += 1
            return _token_response()
        channel_calls += 1
        if channel_calls == 1:
            return _json_response(401, {"message": "invalid access token"})
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    result = await client.get_livestreams(["123"])

    assert result == []
    assert auth_calls == 2  # initial fetch + forced refresh after the 401
    assert channel_calls == 2


async def test_persistent_401_after_retry_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(401, {"message": "invalid access token"})

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_livestreams(["123"])


async def test_oauth_token_endpoint_failure_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return httpx.Response(500, content="internal error")
        return _json_response(200, {"data": []})

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_livestreams(["123"])


async def test_rate_limited_raises_with_a_default_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(429, {"message": "rate limited"})

    client = _client_with(handler)
    with pytest.raises(RateLimitedError) as exc_info:
        await client.get_livestreams(["123"])

    assert exc_info.value.retry_after is not None


async def test_bad_request_raises_permanent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(400, {"message": "invalid parameters"})

    client = _client_with(handler)
    with pytest.raises(PermanentPlatformError):
        await client.get_livestreams(["not-a-valid-id"])


async def test_not_found_raises_permanent_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return _json_response(404, {"message": "not found"})

    client = _client_with(handler)
    with pytest.raises(PermanentPlatformError):
        await client.get_livestreams(["123"])


async def test_server_error_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        return httpx.Response(500, content="internal error")

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_livestreams(["123"])


async def test_timeout_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_auth_request(request):
            return _token_response()
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_livestreams(["123"])
