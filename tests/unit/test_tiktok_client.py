"""TikTokClient: reading the creator-embed page and sorting failures into
not-found / blocked / retryable, through httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

import platforms.tiktok.client as client_module
from platforms.tiktok.client import DEFAULT_BLOCKED_WAIT_SECONDS, TikTokClient
from tests.unit.tiktok_fixtures import BOT_CHECK_PAGE, page, user, video
from utils.errors import RateLimitedError, TransientPlatformError


def _client(handler) -> TikTokClient:
    return TikTokClient(transport=httpx.MockTransport(handler), min_gap_seconds=0)


async def test_reads_the_account_and_its_videos() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=page([video(2), video(1)]))

    profile = await _client(handler).get_profile("nasa")

    assert profile is not None
    assert profile["user"]["uniqueId"] == "nasa"
    assert [v["desc"] for v in profile["videos"]] == ["caption 2", "caption 1"]
    assert seen[0].url.path == "/embed/@nasa"
    assert "Mozilla" in seen[0].headers["User-Agent"]


@pytest.mark.parametrize("status", [400, 404])
async def test_an_unknown_account_is_none(status: int) -> None:
    assert await _client(lambda r: httpx.Response(status, text=page([], error=True))).get_profile("nobody") is None


async def test_an_error_page_or_one_without_an_account_is_none() -> None:
    assert await _client(lambda r: httpx.Response(200, text=page([], error=True))).get_profile("x") is None
    assert await _client(lambda r: httpx.Response(200, text=page([], account={}))).get_profile("x") is None


async def test_a_private_account_comes_back_flagged() -> None:
    profile = await _client(lambda r: httpx.Response(200, text=page([], account=user(private=True)))).get_profile("nasa")

    assert profile is not None and profile["user"]["privateAccount"] is True


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_blocks_pause_the_platform(status: int) -> None:
    with pytest.raises(RateLimitedError) as caught:
        await _client(lambda r: httpx.Response(status, text="blocked")).get_profile("nasa")

    assert caught.value.retry_after == DEFAULT_BLOCKED_WAIT_SECONDS


async def test_retry_after_is_followed_but_capped() -> None:
    with pytest.raises(RateLimitedError) as caught:
        await _client(lambda r: httpx.Response(429, headers={"retry-after": "90"})).get_profile("nasa")
    assert caught.value.retry_after == 90

    with pytest.raises(RateLimitedError) as caught:
        await _client(lambda r: httpx.Response(429, headers={"retry-after": "999999"})).get_profile("nasa")
    assert caught.value.retry_after == 3600


async def test_a_bot_check_page_is_a_block_not_a_missing_account() -> None:
    with pytest.raises(RateLimitedError):
        await _client(lambda r: httpx.Response(200, text=BOT_CHECK_PAGE)).get_profile("nasa")


async def test_a_redirect_is_a_block() -> None:
    with pytest.raises(RateLimitedError):
        await _client(lambda r: httpx.Response(302, headers={"location": "https://www.tiktok.com/"})).get_profile("nasa")


async def test_server_errors_are_retryable() -> None:
    with pytest.raises(TransientPlatformError) as caught:
        await _client(lambda r: httpx.Response(500)).get_profile("nasa")
    assert not isinstance(caught.value, RateLimitedError)


async def test_timeouts_and_connection_failures_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(TransientPlatformError) as caught:
            await _client(handler).get_profile("nasa")
        assert not isinstance(caught.value, RateLimitedError)


@pytest.mark.parametrize(
    "body",
    [
        '<script id="__FRONTITY_CONNECT_STATE__">not json</script>',
        '<script id="__FRONTITY_CONNECT_STATE__">{"source": {"data": {}}}</script>',
        '<script id="__FRONTITY_CONNECT_STATE__">{"source": null}</script>',
    ],
)
async def test_a_page_that_cant_be_read_is_retryable(body: str) -> None:
    with pytest.raises(TransientPlatformError) as caught:
        await _client(lambda r: httpx.Response(200, text=body)).get_profile("nasa")
    assert not isinstance(caught.value, RateLimitedError)


async def test_a_video_list_of_the_wrong_shape_is_retryable() -> None:
    with pytest.raises(TransientPlatformError):
        await _client(lambda r: httpx.Response(200, text=page({"oops": 1}))).get_profile("nasa")


async def test_requests_are_spaced_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    client = TikTokClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page([video(1)]))),
        min_gap_seconds=1.5,
        clock=lambda: now[0],
    )

    await client.get_profile("nasa")
    await client.get_profile("nasa")

    assert sleeps == [1.5]
