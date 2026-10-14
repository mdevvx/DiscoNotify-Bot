"""Reads an X (Twitter) account's latest posts from X's public embedded
timeline — the feed behind the "embed a profile" widget websites use
(syndication.twitter.com). No login, no API key, no cost.

The page is HTML with the timeline as JSON in its __NEXT_DATA__ script: the
account's latest ~20 posts (retweets included and flagged), newest first,
each with its author (id, @handle, name, avatar), full text and media.

Limits (x-rate-limit-* headers): 30 requests per 15 minutes per IP address,
counted separately for lookups by @handle and by numeric user ID (verified
2026-10-15). Adding an account uses the @handle form; routine checks use
the user-ID form, so checks never use up what adding needs.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

_BASE_URL = "https://syndication.twitter.com"
_FXTWITTER_URL = "https://api.fxtwitter.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 900.0
BY_HANDLE = "handle"
BY_ID = "id"


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
        # X keeps a separate 30-per-15-minutes limit for each way of asking
        # (by @handle, by user ID). Last seen x-rate-limit-* values per kind.
        self._remaining: Dict[str, int] = {}
        self._resets_at: Dict[str, float] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup_user(self, handle: str) -> Optional[Dict[str, Any]]:
        """@handle → the account's ID, name and avatar via FxTwitter's free
        public API (api.fxtwitter.com, the service behind fxtwitter.com link
        previews) — so adding an account doesn't depend on X's @handle
        window, which other apps on a shared host IP can use up. None if the
        account doesn't exist (FxTwitter answers that with a redirect)."""
        try:
            response = await self._http.get(f"{_FXTWITTER_URL}/{handle}", follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"FxTwitter lookup failed for @{handle}: {exc}") from exc
        if response.status_code in (301, 302, 404):
            return None
        if response.status_code != 200:
            raise TransientPlatformError(f"FxTwitter lookup error {response.status_code} for @{handle}")
        try:
            user = response.json().get("user") or {}
        except ValueError as exc:
            raise TransientPlatformError(f"FxTwitter lookup for @{handle} couldn't be read") from exc
        return user if user.get("id") else None

    async def get_recent_posts_fx(self, handle: str) -> List[Dict[str, Any]]:
        """The account's latest posts from FxTwitter
        (api.fxtwitter.com/2/profile/{handle}/statuses), converted to the
        same shape as X's embedded timeline. Used for accounts whose X
        embedded timeline comes back empty — seen for some newer accounts
        (2026-10-15: @Cookedcom, 627 posts, empty embed)."""
        try:
            response = await self._http.get(f"{_FXTWITTER_URL}/2/profile/{handle}/statuses", follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"FxTwitter posts request failed for @{handle}: {exc}") from exc
        if response.status_code in (301, 302, 404):
            return []
        if response.status_code == 429:
            raise busy_error(_DEFAULT_RATE_LIMIT_WAIT_SECONDS / 3)
        if response.status_code != 200:
            raise TransientPlatformError(f"FxTwitter posts error {response.status_code} for @{handle}")
        try:
            results = response.json().get("results") or []
        except ValueError as exc:
            raise TransientPlatformError(f"FxTwitter posts for @{handle} couldn't be read") from exc
        return [_from_fxtwitter(status) for status in results if status.get("id")]

    async def get_timeline_by_handle(self, handle: str) -> List[Dict[str, Any]]:
        """For adding an account (its user ID isn't known yet)."""
        return await self._get(BY_HANDLE, f"/srv/timeline-profile/screen-name/{handle}", f"@{handle}")

    async def get_timeline_by_id(self, user_id: str) -> List[Dict[str, Any]]:
        """For routine checks — its own rate limit, separate from adding, and
        unaffected by the account changing its @handle."""
        return await self._get(BY_ID, f"/srv/timeline-profile/user-id/{user_id}", f"user {user_id}")

    def requests_remaining(self, kind: str) -> Optional[int]:
        """None until the first response for this kind, or after its window reset."""
        if self.seconds_until_reset(kind) <= 1.0 and kind in self._resets_at:
            return None
        return self._remaining.get(kind)

    def seconds_until_reset(self, kind: str) -> float:
        resets_at = self._resets_at.get(kind)
        if resets_at is None:
            return _DEFAULT_RATE_LIMIT_WAIT_SECONDS
        return max(resets_at - time.time(), 1.0)

    async def _get(self, kind: str, path: str, who: str) -> List[Dict[str, Any]]:
        """The account's latest posts (raw tweet objects), newest first.
        An empty list means the account has no public posts, or doesn't
        exist / is protected / is suspended — X shows all of those the same
        way."""
        try:
            response = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"X timeline request timed out: {who}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"X timeline request failed: {who}: {exc}") from exc

        self._read_rate_limit(kind, response)
        if response.status_code == 429:
            raise busy_error(self.seconds_until_reset(kind))
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise TransientPlatformError(f"X timeline error {response.status_code} for {who}")
        return _parse_timeline(response.text, who)

    def _read_rate_limit(self, kind: str, response: httpx.Response) -> None:
        try:
            self._remaining[kind] = int(response.headers["x-rate-limit-remaining"])
            self._resets_at[kind] = float(response.headers["x-rate-limit-reset"])
        except (KeyError, ValueError):
            pass


def busy_error(retry_after: float) -> RateLimitedError:
    minutes = max(1, round(retry_after / 60))
    return RateLimitedError(
        f"X is limiting requests right now — try again in about {minutes} minute{'s' if minutes != 1 else ''}",
        retry_after=retry_after,
    )


def _parse_timeline(html: str, who: str) -> List[Dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        # X changed the page — a code problem, not a bad account.
        raise TransientPlatformError(f"X timeline page for {who} has no timeline data (page format changed?)")
    try:
        page = json.loads(match.group(1))
        entries = page["props"]["pageProps"]["timeline"]["entries"] or []
    except (ValueError, KeyError, TypeError) as exc:
        raise TransientPlatformError(f"X timeline page for {who} couldn't be read: {exc}") from exc
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


def _from_fxtwitter(status: Dict[str, Any]) -> Dict[str, Any]:
    """An FxTwitter status in X's embedded-timeline shape, so one parser
    handles both. FxTwitter's text already has real links instead of t.co
    and no trailing media link."""
    author = status.get("author") or {}
    media = status.get("media") or {}
    items = []
    for photo in media.get("photos") or []:
        items.append({"type": "photo", "media_url_https": photo.get("url")})
    for video in media.get("videos") or []:
        items.append({"type": video.get("type") or "video", "media_url_https": video.get("thumbnail_url")})
    post: Dict[str, Any] = {
        "id_str": str(status["id"]),
        "created_at": status.get("created_at"),
        "full_text": status.get("text") or "",
        "user": {
            "id_str": str(author.get("id") or ""),
            "screen_name": author.get("screen_name"),
            "name": author.get("name"),
            "profile_image_url_https": author.get("avatar_url"),
        },
        "entities": {},
        "is_quote_status": bool(status.get("quote")),
    }
    if items:
        post["extended_entities"] = {"media": items}
    if status.get("reposted_by"):
        post["retweeted_status"] = {"by": status["reposted_by"]}
    if status.get("replying_to"):
        post["in_reply_to_status_id_str"] = str(status.get("replying_to_status") or "reply")
    return post
