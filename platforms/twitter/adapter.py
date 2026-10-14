"""X (Twitter) adapter — new-post alerts from X's public embedded timeline
(see client.py). Free: no API key, no login.

The one constraint is X's limit of 30 requests per 15 minutes per IP
address — counted separately for lookups by @handle and by user ID. So:
- routine checks read the timeline by user ID; adding an account looks it
  up by @handle — checks never use up what adding needs, and an account
  that changes its @handle keeps working;
- each X account is fetched once per check and shared by every server that
  follows it;
- the check interval stretches with the number of distinct accounts, using
  about 20 of the 30 (a request sometimes counts twice): 45s per account —
  10 accounts → every 7.5 min, 50 → every 37.5 min;
- when X says a window is (nearly) used up, that kind of request waits for
  the reset instead of hitting 429s.

Only original posts are alerted (quote posts included; retweets and replies
left out). Private, suspended and never-posted accounts can't be added.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, FrozenSet, List, Optional

from events.models import NormalizedEvent
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.twitter import parser
from platforms.twitter.client import BY_HANDLE, BY_ID, TwitterClient, busy_error, require_posts
from utils.errors import PermanentPlatformError, TransientPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_WINDOW_SECONDS = 900
_REQUESTS_PER_WINDOW_FOR_CHECKS = 20
# Left unused in the checks' window, since a request sometimes counts twice.
_KEEP_SPARE = 1
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
        """@handle → ID through FxTwitter, then the timeline by ID (the
        checks' window, which a shared host IP leaves mostly free). Falls
        back to X's by-@handle timeline if FxTwitter is down."""
        handle = parser.parse_identifier(identifier)
        try:
            user = await self._client.lookup_user(handle)
        except TransientPlatformError as exc:
            logger.warning("FxTwitter lookup failed (%s) — looking @%s up on X instead.", exc, handle)
            self._check_window(BY_HANDLE, reserve=0)
            self._count_request()
            posts = await self._client.get_timeline_by_handle(handle)
        else:
            if user is None:
                raise PermanentPlatformError(f"There's no X account @{handle} — check the handle.")
            if user.get("protected"):
                raise PermanentPlatformError(f"@{handle} is a private account — its posts can't be read.")
            self._check_window(BY_ID, reserve=0)
            self._count_request()
            posts = await self._client.get_timeline_by_id(str(user["id"]))
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

        self._check_window(BY_ID, reserve=_KEEP_SPARE)
        self._count_request()
        posts = await self._client.get_timeline_by_id(key)
        self._snapshots[key] = _Snapshot(self._clock(), posts)
        return posts

    def _check_window(self, kind: str, *, reserve: int) -> None:
        remaining = self._client.requests_remaining(kind)
        if remaining is not None and remaining <= reserve:
            raise busy_error(self._client.seconds_until_reset(kind))
