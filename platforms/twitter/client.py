"""Reads an X (Twitter) account's latest posts from X's public embedded
timeline — the feed behind the "embed a profile" widget websites use
(syndication.twitter.com). No login, no API key, no cost.

The page is HTML with the timeline as JSON in its __NEXT_DATA__ script: the
account's latest ~20 posts (retweets included and flagged), newest first,
each with its author (id, @handle, name, avatar), full text and media.

Limit: 30 requests per 15 minutes per IP address, reported in the usual
x-rate-limit-* headers. The adapter spreads its checks to stay under it.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

_BASE_URL = "https://syndication.twitter.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 900.0


class TwitterClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """`transport` is for tests (httpx.MockTransport) only."""
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            follow_redirects=True,
        )
        # From the last response's x-rate-limit-* headers.
        self.requests_remaining: Optional[int] = None
        self.window_resets_at: Optional[float] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_timeline(self, handle: str) -> List[Dict[str, Any]]:
        """The account's latest posts (raw tweet objects), newest first.
        An empty list means the account has no public posts, or doesn't
        exist / is protected / is suspended — X shows all of those the same
        way."""
        path = f"/srv/timeline-profile/screen-name/{handle}"
        try:
            response = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"X timeline request timed out: @{handle}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"X timeline request failed: @{handle}: {exc}") from exc

        self._read_rate_limit(response)
        if response.status_code == 429:
            raise RateLimitedError("X timeline rate limit reached", retry_after=self.seconds_until_reset())
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise TransientPlatformError(f"X timeline error {response.status_code} for @{handle}")
        return _parse_timeline(response.text, handle)

    def seconds_until_reset(self) -> float:
        if self.window_resets_at is None:
            return _DEFAULT_RATE_LIMIT_WAIT_SECONDS
        return max(self.window_resets_at - time.time(), 1.0)

    def _read_rate_limit(self, response: httpx.Response) -> None:
        try:
            self.requests_remaining = int(response.headers["x-rate-limit-remaining"])
            self.window_resets_at = float(response.headers["x-rate-limit-reset"])
        except (KeyError, ValueError):
            pass


def _parse_timeline(html: str, handle: str) -> List[Dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        # X changed the page — a code problem, not a bad account.
        raise TransientPlatformError(f"X timeline page for @{handle} has no timeline data (page format changed?)")
    try:
        page = json.loads(match.group(1))
        entries = page["props"]["pageProps"]["timeline"]["entries"] or []
    except (ValueError, KeyError, TypeError) as exc:
        raise TransientPlatformError(f"X timeline page for @{handle} couldn't be read: {exc}") from exc
    return [entry["content"]["tweet"] for entry in entries if entry.get("type") == "tweet"]


def require_posts(posts: List[Dict[str, Any]], handle: str) -> Dict[str, Any]:
    """The account's profile from its posts, for adding it."""
    if not posts:
        raise PermanentPlatformError(
            f"Couldn't find public posts for @{handle} — check the handle. Accounts that are private, "
            "suspended or have never posted can't be monitored."
        )
    for post in posts:
        user = post.get("user") or {}
        if str(user.get("screen_name", "")).lower() == handle.lower():
            return user
    return posts[0].get("user") or {}
