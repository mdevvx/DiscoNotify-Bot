"""Thin async HTTP client for the Instagram Graph API "Business Discovery"
call this adapter uses. Authenticates with a Page access token belonging to
the bot operator's OWN Facebook Page + linked Instagram professional
account — see platforms/instagram/adapter.py's module docstring (and the
README's Instagram setup steps) for how that token is obtained and what it
can/can't monitor.

Errors are classified by Meta's own error code, not the HTTP status: the
Graph API answers almost every failure — an expired token, a missing
permission, an unknown username, a rate limit — with HTTP 400 (verified
2026-10-12). Treating every 400 as "bad account" would mark every monitored
account invalid the moment the operator's token expires.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://graph.facebook.com/v26.0"
# The most recent media items fetched per call. Meta's usage counts
# processing time as well as calls, so the page is kept small — 5 posts
# between two checks (minutes apart) is already an unusual burst.
_MEDIA_PAGE_SIZE = 5
_MEDIA_FIELDS = "id,caption,timestamp,media_type,media_product_type,media_url,thumbnail_url,permalink"

# Meta's rate-limit error codes: 4 (app), 17 (user), 32 (page), 613
# (custom), 80002 (Instagram business use case). None carries a
# Retry-After; app-level usage is a rolling one-hour window.
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
# How long to pause after a rate limit when Meta doesn't say: 15 minutes the
# first time, doubling while it keeps happening (30, then 60), reset by the
# next successful call. When Meta does say (the business-use-case header's
# estimated_time_to_regain_access, in minutes) that wins.
_RATE_LIMIT_WAIT_SECONDS = 15 * 60.0
_MAX_ESCALATED_WAIT_SECONDS = 60 * 60.0
_MAX_ADVISED_WAIT_SECONDS = 6 * 60 * 60.0
_ADVISED_WAIT_PADDING_SECONDS = 60.0
_INVALID_TOKEN_CODE = 190
# 10/200-299: the token lacks a permission the call needs.
_PERMISSION_CODES = {10, *range(200, 300)}
# 110 + 2207013: "Cannot find user" — no such username, or not a
# Business/Creator account.
_USER_NOT_FOUND = (110, 2207013)


class InstagramClient:
    def __init__(
        self,
        access_token: str,
        business_account_id: str,
        *,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`transport` is exposed purely for tests (httpx.MockTransport) to
        drive this client's error-classification logic without a real
        network call — production code never passes it. `business_account_id`
        is the operator's OWN Instagram professional account's IG user ID —
        Business Discovery is queried as "my account, looking up someone
        else's public data," not with the target's own ID."""
        self._access_token = access_token
        self._business_account_id = business_account_id
        self._http = httpx.AsyncClient(base_url=_BASE_URL, timeout=timeout_seconds, transport=transport)
        # Meta's x-app-usage header: the highest of call count / CPU time /
        # total time, as a percentage of the app's hourly limit.
        self.app_usage_percent: Optional[int] = None
        self._clock = clock
        self._usage_read_at: Optional[float] = None
        # Rate limits in a row without a successful call between them.
        self._consecutive_rate_limits = 0

    def app_usage_age(self) -> Optional[float]:
        """Seconds since Meta last reported `app_usage_percent` (None if it
        never has). The figure only changes when a call is made, so an old
        one says little about the window's state now."""
        return None if self._usage_read_at is None else max(self._clock() - self._usage_read_at, 0.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_business_discovery(self, username: str) -> Optional[Dict[str, Any]]:
        """Another Business/Creator account's public profile + recent media,
        by username — no authorization from that account needed. Returns
        None if Meta returns no business_discovery object at all;
        a username Meta can't find raises PermanentPlatformError."""
        fields = (
            f"business_discovery.username({username})"
            f"{{id,username,name,profile_picture_url,media.limit({_MEDIA_PAGE_SIZE}){{{_MEDIA_FIELDS}}}}}"
        )
        data = await self._get(f"/{self._business_account_id}", {"fields": fields})
        discovery = data.get("business_discovery")
        if discovery is None:
            return None

        media_items: List[Dict[str, Any]] = discovery.get("media", {}).get("data", [])
        return {**discovery, "media": media_items}

    async def get_token_info(self) -> Dict[str, Any]:
        """Meta's debug_token data for this client's own token: is_valid,
        type, expires_at (0 = never), data_access_expires_at, scopes."""
        data = await self._get("/debug_token", {"input_token": self._access_token})
        return data.get("data", {})

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_params = {**params, "access_token": self._access_token}
        try:
            response = await self._http.get(path, params=request_params)
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"Instagram API request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"Instagram API request failed: {path}: {exc}") from exc

        self._record_usage(response)
        if response.status_code == 200:
            self._consecutive_rate_limits = 0
            return response.json()

        self._raise_for_error(response)
        raise AssertionError("unreachable")  # _raise_for_error always raises

    def _record_usage(self, response: httpx.Response) -> None:
        header = response.headers.get("x-app-usage")
        if not header:
            return
        try:
            usage = json.loads(header)
            self.app_usage_percent = max(int(value) for value in usage.values())
            self._usage_read_at = self._clock()
        except (ValueError, TypeError, AttributeError):
            pass

    def _rate_limit_wait(self, response: httpx.Response) -> float:
        """How long to stay away after a rate limit: what Meta says, else an
        escalating wait."""
        buc_header = response.headers.get("x-business-use-case-usage")
        advised = _advised_wait_seconds(buc_header)
        escalated = min(_RATE_LIMIT_WAIT_SECONDS * (2**self._consecutive_rate_limits), _MAX_ESCALATED_WAIT_SECONDS)
        if self._consecutive_rate_limits == 0:
            # Which headers Meta really sends with a rate limit isn't documented
            # for Business Discovery — keep a sample in the log.
            logger.warning(
                "Instagram rate limit — x-app-usage=%s x-business-use-case-usage=%s",
                response.headers.get("x-app-usage"),
                (buc_header or "")[:300] or None,
            )
        self._consecutive_rate_limits += 1
        if advised is not None:
            return min(advised + _ADVISED_WAIT_PADDING_SECONDS, _MAX_ADVISED_WAIT_SECONDS)
        return escalated

    def _raise_for_error(self, response: httpx.Response) -> None:
        error: Dict[str, Any] = {}
        try:
            error = response.json().get("error", {}) or {}
        except ValueError:
            pass  # non-JSON error body
        code = error.get("code")
        subcode = error.get("error_subcode")
        message = error.get("error_user_msg") or error.get("message") or response.text

        if response.status_code == 429 or code in _RATE_LIMIT_CODES:
            raise RateLimitedError(f"Instagram API rate limited: {message}", retry_after=self._rate_limit_wait(response))

        if code == _INVALID_TOKEN_CODE:
            # The operator's credential, not the monitored account — never
            # permanent, or every Instagram account would be marked invalid.
            raise TransientPlatformError(
                f"Instagram access token is invalid or expired — generate a new one (see README): {message}"
            )

        if code in _PERMISSION_CODES and "on behalf of user" in str(message):
            # Meta refuses the Facebook account itself for this app — even
            # debug_token fails — not a missing scope (seen 2026-10-13/15).
            raise TransientPlatformError(
                "Meta is refusing the Facebook account behind INSTAGRAM_ACCESS_TOKEN for this app — most likely the "
                "Meta app isn't in Live mode (App Dashboard → switch Development to Live); otherwise the account "
                "removed the app (Facebook Settings → Business integrations), has a security checkpoint to clear, "
                f"or Meta restricted the app (App Dashboard alerts): {message}"
            )

        if code in _PERMISSION_CODES:
            raise TransientPlatformError(
                f"Instagram access token is missing a permission (Business Discovery needs instagram_basic, "
                f"instagram_manage_insights and pages_read_engagement): {message}"
            )

        if (code, subcode) == _USER_NOT_FOUND:
            raise PermanentPlatformError(
                f"Instagram account not found — wrong username, renamed, or not a Business/Creator account: {message}",
                user_message="We couldn't find that Instagram account. It has to be a public Business or Creator "
                "account — please check the username.",
            )

        if response.status_code >= 500 or error.get("is_transient"):
            raise TransientPlatformError(f"Instagram API error {response.status_code}: {message}")

        # Anything else (an unexpected 4xx) is most likely this request's
        # fault rather than the account's — retry/record as a failure, but
        # don't mark the account invalid on a guess.
        raise TransientPlatformError(f"Instagram API error {response.status_code} (code {code}): {message}")


def _advised_wait_seconds(header: Optional[str]) -> Optional[float]:
    """The longest `estimated_time_to_regain_access` (minutes) in Meta's
    x-business-use-case-usage header, as seconds; None if it's absent or 0."""
    if not header:
        return None
    try:
        data = json.loads(header)
        minutes = [
            float(entry.get("estimated_time_to_regain_access") or 0)
            for entries in data.values()
            for entry in entries
            if isinstance(entry, dict)
        ]
    except (ValueError, TypeError, AttributeError):
        return None
    longest = max(minutes, default=0.0)
    return longest * 60.0 if longest > 0 else None
