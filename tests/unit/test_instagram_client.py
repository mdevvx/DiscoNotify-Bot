"""InstagramClient's error classification (status code -> the right
PlatformError subclass) and business_discovery response unwrapping,
driven through httpx.MockTransport so no real network call is ever made."""

from __future__ import annotations

import json

import httpx
import pytest

from platforms.instagram.client import InstagramClient
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError


def _client_with(handler) -> InstagramClient:
    return InstagramClient("fake-token", "17800000000000000", transport=httpx.MockTransport(handler))


def _json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body))


async def test_get_business_discovery_unwraps_the_nested_media_edge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "business_discovery": {
                    "id": "17841400",
                    "username": "somecreator",
                    "media": {"data": [{"id": "1"}, {"id": "2"}], "paging": {}},
                },
                "id": "17800000000000000",
            },
        )

    client = _client_with(handler)
    discovery = await client.get_business_discovery("somecreator")

    assert discovery is not None
    assert discovery["username"] == "somecreator"
    assert discovery["media"] == [{"id": "1"}, {"id": "2"}]


async def test_get_business_discovery_returns_none_when_field_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"id": "17800000000000000"})

    client = _client_with(handler)
    discovery = await client.get_business_discovery("somecreator")

    assert discovery is None


async def test_request_sends_access_token_and_fields_params() -> None:
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return _json_response(200, {"business_discovery": {"id": "1", "media": {"data": []}}})

    client = _client_with(handler)
    await client.get_business_discovery("somecreator")

    assert seen_params["access_token"] == "fake-token"
    assert "business_discovery.username(somecreator)" in seen_params["fields"]


def _graph_error(code: int, message: str, *, subcode: int = None, status: int = 400) -> httpx.Response:
    """Meta answers nearly every failure with HTTP 400 and puts the real
    reason in error.code — shapes below are the ones the live API returned
    on 2026-10-12."""
    error = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return _json_response(status, {"error": error})


async def test_unknown_username_is_permanent() -> None:
    client = _client_with(lambda request: _graph_error(110, "Invalid user id", subcode=2207013))

    with pytest.raises(PermanentPlatformError):
        await client.get_business_discovery("nonexistent")


async def test_expired_token_is_not_blamed_on_the_account() -> None:
    """Regression test: an invalid/expired token comes back as HTTP 400
    (code 190), and every 400 used to be treated as "this account is
    invalid" — so an expired token would have marked every monitored
    Instagram account invalid."""
    client = _client_with(lambda request: _graph_error(190, "Invalid OAuth access token - Cannot parse access token"))

    with pytest.raises(TransientPlatformError) as exc_info:
        await client.get_business_discovery("somecreator")

    assert not isinstance(exc_info.value, PermanentPlatformError)
    assert "token" in str(exc_info.value)


async def test_missing_permission_is_not_blamed_on_the_account() -> None:
    client = _client_with(lambda request: _graph_error(10, "(#10) Application does not have permission for this action"))

    with pytest.raises(TransientPlatformError) as exc_info:
        await client.get_business_discovery("somecreator")

    assert "instagram_manage_insights" in str(exc_info.value)


@pytest.mark.parametrize("code", [4, 17, 32, 613, 80002])
async def test_meta_rate_limit_codes_raise_rate_limited(code: int) -> None:
    client = _client_with(lambda request: _graph_error(code, "Application request limit reached"))

    with pytest.raises(RateLimitedError):
        await client.get_business_discovery("somecreator")


async def test_an_unexpected_bad_request_is_not_permanent() -> None:
    client = _client_with(lambda request: _graph_error(100, "Unsupported get request."))

    with pytest.raises(TransientPlatformError) as exc_info:
        await client.get_business_discovery("somecreator")

    assert not isinstance(exc_info.value, PermanentPlatformError)


async def test_app_usage_header_is_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"business_discovery": {"id": "1", "media": {"data": []}}}),
            headers={"x-app-usage": '{"call_count":15,"total_cputime":2,"total_time":24}'},
        )

    client = _client_with(handler)
    await client.get_business_discovery("somecreator")

    assert client.app_usage_percent == 24


async def test_requests_reel_and_profile_fields() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return _json_response(200, {"business_discovery": {"id": "1", "media": {"data": []}}})

    await _client_with(handler).get_business_discovery("somecreator")

    for field in ("profile_picture_url", "media_product_type", "thumbnail_url"):
        assert field in seen["fields"]


async def test_rate_limited_raises_with_a_default_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(429, {"error": {"message": "rate limited"}})

    client = _client_with(handler)
    with pytest.raises(RateLimitedError) as exc_info:
        await client.get_business_discovery("somecreator")

    assert exc_info.value.retry_after is not None


async def test_server_error_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content="internal error")

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_business_discovery("somecreator")


async def test_timeout_raises_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client_with(handler)
    with pytest.raises(TransientPlatformError):
        await client.get_business_discovery("somecreator")
