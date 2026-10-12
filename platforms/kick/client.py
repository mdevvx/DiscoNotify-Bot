"""Thin async HTTP client for the Kick Public API calls this adapter uses.

Authenticates with an app access token (OAuth client-credentials grant) —
see platforms/kick/adapter.py's module docstring for why that's enough for
every call made here. The token is fetched lazily on first use, cached
until shortly before its reported expiry, and force-refreshed once if a
call unexpectedly 401s (e.g. the app's client secret was regenerated in
the Kick dev console). Mirrors platforms/twitch/client.py's structure —
Kick's own OAuth/API shape is close enough to Twitch's that the same
approach applies.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_AUTH_URL = "https://id.kick.com/oauth/token"
_BASE_URL = "https://api.kick.com/public/v1"

# Refresh this many seconds before the token's reported expiry, so an
# in-flight request never races the exact expiry instant.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0
# Kick's public API docs don't document a concrete rate limit or a 429
# retry-after header (unlike Twitch's Ratelimit-Reset) — fall back to a
# fixed, generous wait if a 429 is ever actually returned.
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0
# Kick accepts up to 50 broadcaster IDs per /livestreams call.
MAX_IDS_PER_CALL = 50


class KickClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """`transport` is exposed purely for tests (httpx.MockTransport) to
        drive this client's error-classification/token logic without a
        real network call — production code never passes it. No base_url
        is set since this client talks to two hosts (api.kick.com and
        id.kick.com): full URLs are passed to every request instead."""
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_channel_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """None for a slug Kick doesn't know. Kick answers that with HTTP 400
        "Invalid request" (verified 2026-10-12), not an empty list."""
        try:
            data = await self._get("/channels", {"slug": slug})
        except PermanentPlatformError:
            return None
        items = data.get("data") or []
        return items[0] if items else None

    async def get_livestreams(self, broadcaster_user_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """The currently-live streams among these broadcasters — one call for
        up to 50. A broadcaster who's offline is simply absent (verified
        2026-10-12). Each item carries title, thumbnail, started_at,
        category, viewer_count and the streamer's profile_picture."""
        ids = list(broadcaster_user_ids)[:MAX_IDS_PER_CALL]
        if not ids:
            return []
        data = await self._get("/livestreams", [("broadcaster_user_id", value) for value in ids])
        return data.get("data") or []

    async def _get(self, path: str, params: Any, *, _retried: bool = False) -> Dict[str, Any]:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            response = await self._http.get(f"{_BASE_URL}{path}", params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"Kick API request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"Kick API request failed: {path}: {exc}") from exc

        if response.status_code == 200:
            return response.json()

        if response.status_code == 401 and not _retried:
            # Our cached token looked valid but Kick rejected it anyway —
            # force one refresh and retry before giving up.
            logger.info("Kick API returned 401 with a cached token — forcing a refresh and retrying once.")
            self._access_token = None
            return await self._get(path, params, _retried=True)

        self._raise_for_error(response)
        raise AssertionError("unreachable")  # _raise_for_error always raises

    async def _ensure_token(self) -> None:
        if self._access_token is not None and time.monotonic() < self._token_expires_at:
            return

        try:
            response = await self._http.post(
                _AUTH_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.TimeoutException as exc:
            raise TransientPlatformError("Kick OAuth token request timed out") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"Kick OAuth token request failed: {exc}") from exc

        if response.status_code != 200:
            raise TransientPlatformError(f"Kick OAuth token request failed ({response.status_code}): {response.text}")

        body = response.json()
        self._access_token = body["access_token"]
        expires_in = body.get("expires_in", 3600)
        self._token_expires_at = time.monotonic() + max(expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 0)

    def _raise_for_error(self, response: httpx.Response) -> None:
        message = response.text
        try:
            body = response.json()
            message = body.get("message", message)
        except ValueError:
            pass  # non-JSON error body; fall back to the raw text already set above

        if response.status_code == 429:
            raise RateLimitedError(f"Kick API rate limited: {message}", retry_after=_DEFAULT_RATE_LIMIT_WAIT_SECONDS)

        if response.status_code == 400:
            raise PermanentPlatformError(f"Kick API rejected the request: {message}")

        if response.status_code in {401, 403}:
            # Auth/permission trouble (bad client id/secret) rather than a
            # bad request for a specific account — a configuration problem,
            # not evidence this specific account is invalid. Same reasoning
            # as YouTubeClient's/TwitchClient's 401/403 handling.
            raise TransientPlatformError(f"Kick API auth/permission error ({response.status_code}): {message}")

        if response.status_code == 404:
            raise PermanentPlatformError(f"Kick resource not found: {message}")

        raise TransientPlatformError(f"Kick API error {response.status_code}: {message}")
