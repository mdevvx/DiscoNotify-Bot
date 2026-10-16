"""Instagram Graph API adapter — new-post (and Reel) detection via
"Business Discovery".

API research first done 2026-09-15 and re-verified against the live API on
2026-10-12, not scraped/undocumented endpoints — see
[[project-platform-api-policy]] / README.md for the standing policy this
follows, and the README's Instagram setup steps for getting credentials.

| Feature          | Method                                                          | Notes |
|------------------|-----------------------------------------------------------------|-------|
| New posts/Reels  | GET /{your_ig_user_id}?fields=business_discovery.username(...) | 1 call per account check; posts and Reels told apart by media_product_type (FEED/REELS) |
| Stories          | —                                                               | Not exposed for other accounts by any official API |
| Live             | —                                                               | Not exposed for other accounts by any official API |

Constraints that make this different from the other adapters:
1. **The target must be a Business or Creator account.** Personal
   accounts can't be monitored by any official API (Meta closed public
   account discovery in 2020); Meta answers "Cannot find user".
2. **The operator needs their own setup**: a Meta app (Business type, with
   Facebook Login for Business), a Facebook Page linked to their own
   Instagram professional account, and a Page token with instagram_basic,
   instagram_manage_insights, pages_read_engagement and pages_show_list.
   Verified 2026-10-12: instagram_manage_insights is required — without it
   every lookup fails with "(#10) Application does not have permission".
   With it, lookups of arbitrary public Business/Creator accounts work
   while the app is still in Development mode (the operator holds an app
   role), so no App Review is needed for the operator's own use.
3. **Lookup is by username only** — there's no by-ID variant. If a
   monitored account renames its handle, Meta answers "Cannot find user",
   the account is marked invalid, and it has to be re-added.
4. **Rate limit**: in Development mode the app gets 200 calls per hour in
   total (measured 2026-10-12 via the x-app-usage header: ~30 calls = 15%).
   Every monitored account costs one call per check, so the poll interval
   stretches with the number of distinct accounts, and slows further if
   Meta reports usage getting high (see poll_interval_seconds()).
5. **No webhook exists** for accounts the operator doesn't manage — this
   is a poll by necessity.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from events.models import NormalizedEvent
from monitoring.rate_limiter import RateLimiter
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.instagram import parser
from platforms.instagram.client import InstagramClient
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

# Fraction of the hourly call limit the cadence is sized against — the rest
# is headroom for account-add lookups and restarts (the in-memory budget
# starts empty again on every restart, Meta's window doesn't).
_BUDGET_HEADROOM = 0.75
# Warn at startup when the token (or Meta's separate data-access window)
# ends within this long.
_TOKEN_EXPIRY_WARNING = datetime.timedelta(days=14)
_REQUIRED_SCOPES = frozenset({"instagram_basic", "instagram_manage_insights", "pages_read_engagement"})
_CURSOR_SEPARATOR = "|"


@dataclass
class _Snapshot:
    fetched_at: float
    discovery: Dict[str, Any]


# Adaptive cadence: how recently an account posted decides how often it's
# actually looked up — (posted within, check at least this far apart).
# Anything quieter (or with no posts) is checked every _QUIET_INTERVAL.
_ACTIVITY_TIERS: Tuple[Tuple[datetime.timedelta, float], ...] = (
    (datetime.timedelta(hours=1), 0.0),  # the scheduler's normal interval
    (datetime.timedelta(hours=6), 120.0),
)
_QUIET_INTERVAL = 240.0


class InstagramAdapter(PlatformAdapter):
    platform: ClassVar[str] = "instagram"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.POSTS})
    counts_own_requests: ClassVar[bool] = True

    def __init__(
        self,
        client: InstagramClient,
        *,
        hourly_call_limit: int = 200,
        min_poll_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
    ) -> None:
        self._client = client
        self._clock = clock
        self._wall_clock = wall_clock
        self._usable_calls_per_hour = max(hourly_call_limit * _BUDGET_HEADROOM, 1.0)
        self._budget = RateLimiter(max_requests=max(int(self._usable_calls_per_hour), 1), period_seconds=3600)
        self._min_poll_interval = min_poll_interval_seconds
        self._poll_interval = min_poll_interval_seconds
        # Two guilds monitoring the same account share one lookup per tick.
        self._snapshots: Dict[str, _Snapshot] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_credentials(self) -> None:
        """Warns about the mistakes that are easy to make getting the token
        (README's Instagram setup): a Page token fetched with a short-lived
        user token expires within hours instead of never, and a token
        missing instagram_manage_insights fails every lookup."""
        info = await self._client.get_token_info()
        if not info.get("is_valid", False):
            logger.warning("INSTAGRAM_ACCESS_TOKEN is invalid — Instagram monitoring will fail. See README setup.")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = info.get("expires_at") or 0
        if expires_at:
            expires = datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc)
            if expires - now < _TOKEN_EXPIRY_WARNING:
                logger.warning(
                    "INSTAGRAM_ACCESS_TOKEN expires %s UTC. Use a Page token fetched with an *extended* user "
                    "token — those don't expire (README Instagram setup, step 'Extend Access Token').",
                    expires.strftime("%Y-%m-%d %H:%M"),
                )
        data_access_expires_at = info.get("data_access_expires_at") or 0
        if data_access_expires_at:
            data_expires = datetime.datetime.fromtimestamp(data_access_expires_at, datetime.timezone.utc)
            if data_expires - now < _TOKEN_EXPIRY_WARNING:
                logger.warning(
                    "Instagram data access for this token ends %s UTC — generate the token again in the Graph API "
                    "Explorer before then to renew it.",
                    data_expires.strftime("%Y-%m-%d"),
                )
        missing = _REQUIRED_SCOPES - set(info.get("scopes") or [])
        if missing:
            logger.warning("INSTAGRAM_ACCESS_TOKEN is missing permission(s): %s.", ", ".join(sorted(missing)))
        if info.get("type") != "PAGE":
            logger.warning("INSTAGRAM_ACCESS_TOKEN is a %s token, not a Page token.", info.get("type"))

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        """One call per account per check against the hourly budget: 1
        account → 30s floor, 5 → 120s, 10 → 240s with the default 200/hour.
        Doubled when Meta reports ≥70% of the hourly limit used, quadrupled
        at ≥90%, so a busy hour slows down instead of hitting the wall."""
        accounts = max(distinct_account_count, 1)
        interval = max(self._min_poll_interval, 3600 * accounts / self._usable_calls_per_hour)
        usage = self._client.app_usage_percent or 0
        if usage >= 90:
            interval *= 4
        elif usage >= 70:
            interval *= 2
        self._poll_interval = interval
        return interval

    async def validate_account(self, identifier: str) -> AccountRef:
        username = parser.parse_identifier(identifier)
        if not username:
            raise PermanentPlatformError(
                "Enter an Instagram username or profile URL.",
                user_message="Enter an Instagram username or profile link.",
            )
        # Counted against the budget but never blocked by it — adding an
        # account is a one-off, interactive action.
        await self._budget.try_acquire()
        self._count_request()
        discovery = await self._client.get_business_discovery(username)
        if discovery is None:
            raise PermanentPlatformError(
                f"No Instagram Business/Creator account found for {identifier!r} — personal accounts can't be "
                "monitored (see README)."
            )
        return parser.discovery_to_account_ref(discovery)

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        snapshot = await self._get_snapshot(account)
        if snapshot is None:
            return FetchResult(items=[], next_cursor=cursor)

        media_items: List[Dict[str, Any]] = snapshot.discovery.get("media", [])
        if not media_items:
            # An account with no posts yet still needs a baseline — without
            # one, its first post was taken as the baseline on the next
            # check and never announced (found 2026-10-12 with an account
            # added before its first post).
            return FetchResult(items=[], next_cursor=cursor or _empty_baseline_cursor())

        newest = max(media_items, key=lambda item: item.get("timestamp") or "")
        next_cursor = _make_cursor(newest)
        if cursor is None:
            # First-ever poll for this account: establish a baseline
            # without notifying about its entire existing post history.
            return FetchResult(items=[], next_cursor=next_cursor)

        new_items = _items_after_cursor(media_items, cursor)
        if new_items is None:
            logger.info(
                "Instagram cursor for %s is no longer on the page and has no timestamp; re-baselining.",
                account.username,
            )
            return FetchResult(items=[], next_cursor=next_cursor)

        profile = {
            "name": snapshot.discovery.get("name"),
            "profile_picture_url": snapshot.discovery.get("profile_picture_url"),
        }
        items = [{**item, "_profile": profile} for item in new_items]
        return FetchResult(items=items, next_cursor=next_cursor)

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        raise NotImplementedError("InstagramAdapter declares no live-status capability — the worker never calls this.")

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        return parser.normalize_media(account, raw_item)

    def interval_for(self, discovery: Dict[str, Any]) -> float:
        """How far apart this account's lookups should be, from how recently
        it last posted: within the hour → the normal interval, within 6 hours
        → 2 minutes, quieter (or no posts) → 4 minutes. Never faster than the
        scheduler's budget-sized interval."""
        newest = _newest_post_time(discovery)
        if newest is not None:
            quiet_for = self._wall_clock() - newest
            for posted_within, interval in _ACTIVITY_TIERS:
                if quiet_for < posted_within:
                    return max(self._poll_interval, interval)
        return max(self._poll_interval, _QUIET_INTERVAL)

    def _due(self, snapshot: "_Snapshot") -> bool:
        # Half a tick of slack so a check lines up with the scheduler's ticks
        # instead of slipping to the one after.
        age = self._clock() - snapshot.fetched_at
        return age >= self.interval_for(snapshot.discovery) - self._poll_interval / 2

    async def _get_snapshot(self, account: AccountRef) -> Optional[_Snapshot]:
        # Keyed by username: Business Discovery has no by-ID lookup, so
        # account.username (not platform_account_id) is the query key.
        key = account.username.lower()
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._snapshots.get(key)
            if cached is not None and not self._due(cached):
                return cached

            if not await self._budget.try_acquire():
                logger.warning("Skipping Instagram check for %s — hourly call budget used up.", account.username)
                return None
            self._count_request()
            discovery = await self._client.get_business_discovery(account.username)
            if discovery is None:
                raise PermanentPlatformError(
                    f"Instagram account {account.username!r} no longer resolves via Business Discovery "
                    "(renamed, deleted, or no longer a professional account)."
                )
            snapshot = _Snapshot(fetched_at=self._clock(), discovery=discovery)
            self._snapshots[key] = snapshot
            return snapshot


def _empty_baseline_cursor() -> str:
    """"|<now>" — no post yet, so anything posted after now is new. Same
    timestamp format Meta uses, so it compares correctly as a string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{_CURSOR_SEPARATOR}{now.strftime('%Y-%m-%dT%H:%M:%S+0000')}"


def _newest_post_time(discovery: Dict[str, Any]) -> Optional[datetime.datetime]:
    times = []
    for item in discovery.get("media", []):
        value = item.get("timestamp")
        if not value:
            continue
        try:
            times.append(datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z"))
        except ValueError:
            continue
    return max(times) if times else None


def _make_cursor(media: Dict[str, Any]) -> str:
    """"<mediaId>|<timestamp>". The timestamp lets a poll pick out newer
    posts even when the cursor's post was deleted — without it, every post
    on the page would look new."""
    timestamp = media.get("timestamp")
    return f"{media['id']}{_CURSOR_SEPARATOR}{timestamp}" if timestamp else media["id"]


def _items_after_cursor(media_items: List[Dict[str, Any]], cursor: str) -> Optional[List[Dict[str, Any]]]:
    """Posts newer than `cursor`, oldest first. None when that can't be
    told (a pre-2026-10-12 bare-ID cursor whose post is gone)."""
    cursor_id, _, cursor_time = cursor.partition(_CURSOR_SEPARATOR)
    if cursor_time:
        newer = [
            item
            for item in media_items
            if item["id"] != cursor_id and _timestamp_key(item.get("timestamp")) > _timestamp_key(cursor_time)
        ]
        return sorted(newer, key=lambda item: _timestamp_key(item.get("timestamp")))

    for index, item in enumerate(media_items):  # newest first
        if item["id"] == cursor_id:
            return list(reversed(media_items[:index]))
    return None


def _timestamp_key(value: Optional[str]) -> Tuple[int, str]:
    # Meta's timestamps share one fixed format ("2026-10-11T20:36:14+0000"),
    # so they order correctly as strings.
    return (1, value) if value else (0, "")
