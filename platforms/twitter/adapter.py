"""X (Twitter) adapter — new-post alerts from X's public embedded timeline
(see client.py). Free: no API key, no login.

The one constraint is X's limit of 30 timeline requests per 15 minutes per
IP address. So:
- each X account is fetched once per check and shared by every server that
  follows it;
- the check interval stretches with the number of distinct accounts, using
  about 20 of the 30 requests (a request sometimes counts twice, and some
  are left for adding accounts):
  45s per account — 10 accounts → every 7.5 min, 50 → every 37.5 min;
- when X says the window is (nearly) used up, the platform waits for the
  reset instead of hitting 429s.

Only original posts are alerted (quote posts included; retweets and replies
left out). Private, suspended and never-posted accounts can't be added. The
timeline is fetched by @handle; if the account renames, it has to be
re-added.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, FrozenSet, List, Optional

from events.models import NormalizedEvent
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.twitter import parser
from platforms.twitter.client import TwitterClient, require_posts
from utils.errors import PermanentPlatformError, RateLimitedError
from utils.logger import get_logger

logger = get_logger(__name__)

_WINDOW_SECONDS = 900
_REQUESTS_PER_WINDOW_FOR_CHECKS = 20
# Requests kept back in each window so adding an account still works.
_RESERVED_FOR_ADDING = 2
# Cursor for an account with no posts yet: everything it posts is new.
_EMPTY_CURSOR = "0"


@dataclass
class _Snapshot:
    fetched_at: float
    posts: List[Dict[str, Any]]


class TwitterAdapter(PlatformAdapter):
    platform: ClassVar[str] = "twitter"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.POSTS})
    counts_own_requests: ClassVar[bool] = True

    def __init__(
        self,
        client: TwitterClient,
        *,
        min_poll_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_poll_interval = min_poll_interval_seconds
        self._poll_interval = min_poll_interval_seconds
        self._clock = clock
        self._snapshots: Dict[str, _Snapshot] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        accounts = max(distinct_account_count, 1)
        self._poll_interval = max(self._min_poll_interval, _WINDOW_SECONDS * accounts / _REQUESTS_PER_WINDOW_FOR_CHECKS)
        return self._poll_interval

    async def validate_account(self, identifier: str) -> AccountRef:
        handle = parser.parse_identifier(identifier)
        posts = await self._fetch(handle, reserve=0)
        ref = parser.user_to_account_ref(require_posts(posts, handle))
        self._snapshots[ref.platform_account_id] = _Snapshot(self._clock(), posts)
        return ref

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        posts = await self._posts_for(account)
        newest = max((int(post["id_str"]) for post in posts), default=None)
        if cursor is None:
            # Just added: everything already there is history, not news.
            return FetchResult(items=[], next_cursor=str(newest) if newest is not None else _EMPTY_CURSOR)

        new = [post for post in parser.original_posts(posts) if int(post["id_str"]) > int(cursor)]
        next_cursor = str(max(newest, int(cursor))) if newest is not None else cursor
        return FetchResult(items=new, next_cursor=next_cursor)

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        raise NotImplementedError("TwitterAdapter declares no live-status capability — the worker never calls this.")

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        return parser.normalize_tweet(account, raw_item)

    async def _posts_for(self, account: AccountRef) -> List[Dict[str, Any]]:
        """One fetch per account per check, shared by every server that
        follows it."""
        key = account.platform_account_id
        snapshot = self._snapshots.get(key)
        if snapshot is not None and self._clock() - snapshot.fetched_at < self._poll_interval / 2:
            return snapshot.posts

        posts = await self._fetch(account.username, reserve=_RESERVED_FOR_ADDING)
        owner = next((post.get("user") or {} for post in posts), {})
        if owner.get("id_str") and str(owner["id_str"]) != key:
            raise PermanentPlatformError(
                f"@{account.username} now belongs to a different X account — the monitored account was renamed. "
                "Remove it and add it again under its new handle."
            )
        self._snapshots[key] = _Snapshot(self._clock(), posts)
        return posts

    async def _fetch(self, handle: str, *, reserve: int) -> List[Dict[str, Any]]:
        remaining = self._client.requests_remaining
        if remaining is not None and remaining <= reserve and self._client.seconds_until_reset() > 1:
            raise RateLimitedError(
                "X timeline requests for this 15-minute window are used up", retry_after=self._client.seconds_until_reset()
            )
        self._count_request()
        return await self._client.get_timeline(handle)
