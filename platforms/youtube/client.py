"""Thin async HTTP client for the specific YouTube Data API v3 REST calls
this adapter uses. Classifies every non-2xx response into one of this
project's PlatformError subclasses, so nothing above this layer needs to
know YouTube's specific error/status-code shapes.

Only a plain API key is used (no OAuth) — every endpoint called here reads
public channel/video data, which the Data API explicitly does not require
user authorization for.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

from monitoring.daily_quota import seconds_until_pacific_midnight
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://www.googleapis.com/youtube/v3"

# videos.list only returns player.embedWidth/embedHeight when a maxHeight
# or maxWidth is passed. Only the aspect ratio matters (it's how the parser
# tells a vertical Short from a landscape video), so any valid value works.
_EMBED_MAX_HEIGHT = 1000

# A quota-exhaustion response doesn't carry a Retry-After header (it isn't
# a per-request rate limit), but the daily quota resets at midnight Pacific,
# so the wait is until then (plus a minute of margin).
_QUOTA_RESET_MARGIN_SECONDS = 60.0


class QuotaExceededError(RateLimitedError):
    """Google says today's quota is gone (not a momentary rate limit): the
    adapter stops spending until the reset."""


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
    ) -> None:
        """`transport` is exposed purely for tests (httpx.MockTransport) to
        drive this client's error-classification logic without a real
        network call — production code never passes it."""
        self._api_key = api_key
        self._clock = clock
        self._http = httpx.AsyncClient(base_url=_BASE_URL, timeout=timeout_seconds, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_channel(
        self,
        *,
        channel_id: Optional[str] = None,
        handle: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"part": "snippet,contentDetails"}
        if channel_id:
            params["id"] = channel_id
        elif handle:
            params["forHandle"] = handle
        elif username:
            params["forUsername"] = username
        else:
            raise ValueError("Provide one of channel_id, handle, or username")

        data = await self._get("/channels", params)
        items = data.get("items", [])
        return items[0] if items else None

    async def list_uploads(self, uploads_playlist_id: str, *, max_results: int = 10) -> List[Dict[str, Any]]:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": max_results,
        }
        data = await self._get("/playlistItems", params)
        return data.get("items", [])

    async def get_videos(self, video_ids: List[str]) -> List[Dict[str, Any]]:
        """Accepts up to 50 IDs per call (still costs 1 unit total)."""
        if not video_ids:
            return []
        params = {
            "part": "snippet,contentDetails,liveStreamingDetails,player",
            "id": ",".join(video_ids[:50]),
            "maxHeight": _EMBED_MAX_HEIGHT,
        }
        data = await self._get("/videos", params)
        return data.get("items", [])

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_params = {**params, "key": self._api_key}
        try:
            response = await self._http.get(path, params=request_params)
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"YouTube API request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"YouTube API request failed: {path}: {exc}") from exc

        if response.status_code == 200:
            return response.json()

        self._raise_for_error(response)
        raise AssertionError("unreachable")  # _raise_for_error always raises

    def _raise_for_error(self, response: httpx.Response) -> None:
        reason = ""
        message = response.text
        try:
            body = response.json()
            errors = body.get("error", {}).get("errors") or []
            reason = errors[0].get("reason", "") if errors else ""
            message = body.get("error", {}).get("message", message)
        except ValueError:
            pass  # non-JSON error body; fall back to the raw text already set above

        if response.status_code == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
            raise RateLimitedError(
                f"YouTube API rate limited: {message}", retry_after=_parse_retry_after(response.headers.get("Retry-After"))
            )

        if reason == "quotaExceeded":
            raise QuotaExceededError(
                f"YouTube API daily quota exceeded: {message}",
                retry_after=seconds_until_pacific_midnight(self._clock()) + _QUOTA_RESET_MARGIN_SECONDS,
            )

        if response.status_code == 404 or reason in {"channelNotFound", "videoNotFound", "playlistItemsNotAccessible"}:
            raise PermanentPlatformError(f"YouTube resource not found: {message}")

        if response.status_code == 400:
            raise PermanentPlatformError(f"YouTube API rejected the request: {message}")

        if response.status_code in {401, 403}:
            # Auth/permission trouble (bad or restricted API key) rather than
            # a bad request for a specific resource — most likely a
            # configuration problem, not something wrong with the specific
            # account being polled. Treated as transient (bounded retries,
            # then recorded as a platform/account failure like any other)
            # rather than permanent, since raising PermanentPlatformError
            # here would incorrectly mark whichever account happened to be
            # polled first as "invalid" when the real problem is the key.
            raise TransientPlatformError(f"YouTube API auth/permission error ({response.status_code}): {message}")

        raise TransientPlatformError(f"YouTube API error {response.status_code}: {message}")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
