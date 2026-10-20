"""Reads a TikTok account's profile and latest videos from TikTok's public
creator embed (tiktok.com/embed/@name) — the page TikTok publishes so
websites can embed a creator's profile. No login, no API key, no cost.

The page is HTML with its data as JSON in a __FRONTITY_CONNECT_STATE__
script: the account (numeric ID, @name, display name, avatar, private flag)
and its latest ~10 videos (ID, caption, cover image), newest first.

TikTok publishes no request limit for it, so requests are kept gentle: one
per account per check, spaced at least a moment apart. When TikTok answers
with a block (403, 429, 503 or a bot-check page) the whole platform waits.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Dict, Optional

import httpx

from utils.errors import RateLimitedError, TransientPlatformError

_BASE_URL = "https://www.tiktok.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
_STATE_RE = re.compile(r'<script id="__FRONTITY_CONNECT_STATE__"[^>]*>(.*?)</script>', re.S)
_EMBED_KEY_PREFIX = "/embed/@"
DEFAULT_BLOCKED_WAIT_SECONDS = 600.0
_MAX_BLOCKED_WAIT_SECONDS = 3600.0
_DEFAULT_MIN_GAP_SECONDS = 1.5
_CONNECT_TIMEOUT_SECONDS = 6.0


class TikTokClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        min_gap_seconds: float = _DEFAULT_MIN_GAP_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`transport` is for tests (httpx.MockTransport) only."""
        if transport is None:
            # IPv4 only: on some networks the IPv6 route to TikTok hangs at
            # connect time, which made about half of all requests time out
            # (seen 2026-10-20) while IPv4 answered in about a second.
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            # A connection that hasn't opened in a few seconds won't; fail fast
            # so the retry gets its turn.
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, _CONNECT_TIMEOUT_SECONDS)),
            transport=transport,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            follow_redirects=False,
        )
        self._min_gap = min_gap_seconds
        self._clock = clock
        self._gate = asyncio.Lock()
        self._last_request_at: Optional[float] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_profile(self, handle: str) -> Optional[Dict[str, Any]]:
        """{"user": <account>, "videos": [<video>, …]} for the @name, or None
        if there is no such account (deleted, renamed, banned).

        A private account comes back as normal, with user["privateAccount"]
        true and no videos. Raises RateLimitedError when TikTok blocks the
        request, TransientPlatformError for anything else that's worth
        trying again."""
        await self._space_out()
        try:
            response = await self._http.get(f"/embed/@{handle}")
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"TikTok request timed out: @{handle}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"TikTok request failed: @{handle}: {type(exc).__name__}: {exc}") from exc

        status = response.status_code
        if status in (400, 404):
            return None
        if status in (403, 429, 503):
            raise blocked_error(_retry_after(response), f"TikTok answered {status} for @{handle}")
        if 300 <= status < 400:
            # Normally a bot check or a region redirect, never a real profile.
            raise blocked_error(None, f"TikTok redirected the request for @{handle} ({status})")
        if status != 200:
            raise TransientPlatformError(f"TikTok error {status} for @{handle}")
        return _parse_embed(response.text, handle)

    async def _space_out(self) -> None:
        """Requests go out at least `min_gap` apart, even when many accounts
        are checked in the same cycle."""
        async with self._gate:
            if self._last_request_at is not None:
                wait = self._min_gap - (self._clock() - self._last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = self._clock()


def blocked_error(retry_after: Optional[float], detail: str) -> RateLimitedError:
    wait = min(retry_after or DEFAULT_BLOCKED_WAIT_SECONDS, _MAX_BLOCKED_WAIT_SECONDS)
    return RateLimitedError(f"TikTok is limiting requests right now ({detail})", retry_after=wait)


def _retry_after(response: httpx.Response) -> Optional[float]:
    try:
        return max(float(response.headers["retry-after"]), 1.0)
    except (KeyError, ValueError):
        return None


def _parse_embed(html: str, handle: str) -> Optional[Dict[str, Any]]:
    match = _STATE_RE.search(html)
    if match is None:
        # A 200 without the data is TikTok's "please wait" bot check (or a
        # changed page) — a block, not a bad account.
        raise blocked_error(None, f"the page for @{handle} had no profile data")
    try:
        data = json.loads(match.group(1))["source"]["data"]
        page = next(value for key, value in data.items() if key.startswith(_EMBED_KEY_PREFIX))
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        raise TransientPlatformError(f"TikTok page for @{handle} couldn't be read: {type(exc).__name__}") from exc

    user = page.get("userInfo") or {}
    if page.get("isError") or not user.get("id") or not user.get("uniqueId"):
        return None
    videos = page.get("videoList") or []
    if not isinstance(videos, list):
        raise TransientPlatformError(f"TikTok video list for @{handle} had an unexpected shape")
    return {"user": user, "videos": videos}
