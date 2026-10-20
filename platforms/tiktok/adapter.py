"""TikTok adapter — new-video alerts from TikTok's public creator embed
(see client.py). Free: no API key, no login.

- Each TikTok account is fetched once per check and shared by every server
  that follows it. The interval is 4s × the number of distinct accounts
  (never under TIKTOK_MIN_POLL_INTERVAL_SECONDS) — 30 accounts → every 2
  min, 150 → every 10 min.
- New videos are those with an ID above the stored cursor; a video ID
  carries its upload time, so no extra request is needed for it.
- The embed shows the latest ~10 videos, so more than that uploaded between
  two checks would miss the oldest — not a concern at these intervals.
- Only videos are alerted: the embed has no live status.

Failures are sorted by what they mean. A block from TikTok pauses the whole
platform (RateLimitedError); timeouts and server errors are retried
(TransientPlatformError). An account TikTok stops showing is only declared
gone after it has been missing in several checks spaced minutes apart, so a
single bad answer can't switch off a working account.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, FrozenSet, Optional, Tuple

from events.models import NormalizedEvent
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.tiktok import parser
from platforms.tiktok.client import TikTokClient
from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_SECONDS_PER_ACCOUNT = 4.0
# Cursor for an account with no videos yet: everything it posts is new.
_EMPTY_CURSOR = "0"
# An account must be missing in this many separate checks, at least
# _STRIKE_SPACING_SECONDS apart, before it's treated as gone for good.
_ADD_ATTEMPTS = 2
_STRIKES_BEFORE_GONE = 3
_STRIKE_SPACING_SECONDS = 300.0


@dataclass
class _Snapshot:
    fetched_at: float
    profile: Optional[Dict[str, Any]]


class TikTokAdapter(PlatformAdapter):
    platform: ClassVar[str] = "tiktok"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.VIDEOS})
    counts_own_requests: ClassVar[bool] = True

    def __init__(
        self,
        client: TikTokClient,
        *,
        min_poll_interval_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_poll_interval = min_poll_interval_seconds
        self._poll_interval = min_poll_interval_seconds
        self._clock = clock
        self._snapshots: Dict[str, _Snapshot] = {}
        # account ID → (checks it has been missing in, when the last one was counted)
        self._missing: Dict[str, Tuple[int, float]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        accounts = max(distinct_account_count, 1)
        self._poll_interval = max(self._min_poll_interval, _SECONDS_PER_ACCOUNT * accounts)
        return self._poll_interval

    async def validate_account(self, identifier: str) -> AccountRef:
        handle = parser.parse_identifier(identifier)
        profile = await self._lookup_for_add(handle)
        if profile is None:
            message = f"There's no TikTok account @{handle} — check the username."
            raise PermanentPlatformError(message, user_message=message)
        if profile["user"].get("privateAccount"):
            message = f"@{handle} is a private account, so its videos can't be seen."
            raise PermanentPlatformError(message, user_message=message)
        ref = parser.user_to_account_ref(profile["user"])
        self._snapshots[ref.platform_account_id] = _Snapshot(self._clock(), profile)
        return ref

    async def _lookup_for_add(self, handle: str) -> Optional[Dict[str, Any]]:
        """A timeout or server error is tried once more before the member is
        told to try again — a blocked request (RateLimitedError) is not,
        since asking again straight away only makes a block last longer."""
        for attempt in range(_ADD_ATTEMPTS):
            self._count_request()
            try:
                return await self._client.get_profile(handle)
            except RateLimitedError:
                raise
            except TransientPlatformError as exc:
                if attempt + 1 >= _ADD_ATTEMPTS:
                    raise
                logger.info("TikTok lookup for @%s failed (%s) — trying once more.", handle, exc)
        raise AssertionError("unreachable")  # pragma: no cover

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        profile = await self._profile_for(account)
        if profile is None or profile["user"].get("privateAccount"):
            self._note_missing(account)
            raise TransientPlatformError(f"TikTok @{account.username} isn't visible right now")
        self._missing.pop(account.platform_account_id, None)

        videos = profile["videos"]
        newest = parser.newest_id(videos)
        if cursor is None:
            # Just added: everything already there is history, not news.
            return FetchResult(items=[], next_cursor=str(newest) if newest is not None else _EMPTY_CURSOR)

        new = [video for video in parser.public_videos(videos) if int(video["id"]) > int(cursor)]
        next_cursor = str(max(newest, int(cursor))) if newest is not None else cursor
        return FetchResult(items=[{"video": video, "user": profile["user"]} for video in new], next_cursor=next_cursor)

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        raise NotImplementedError("TikTokAdapter declares no live-status capability — the worker never calls this.")

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        return parser.normalize_video(account, raw_item)

    async def _profile_for(self, account: AccountRef) -> Optional[Dict[str, Any]]:
        """One fetch per account per check, shared by every server that
        follows it."""
        key = account.platform_account_id
        snapshot = self._snapshots.get(key)
        fresh = snapshot is not None and self._clock() - snapshot.fetched_at < self._poll_interval / 2
        if snapshot is not None and snapshot.profile is not None and fresh:
            return snapshot.profile
        self._count_request()
        profile = await self._client.get_profile(account.username)
        # An account that changed its @name now answers as someone else (or
        # nobody) — only trust the page if it's still this account's ID.
        if profile is not None and str(profile["user"].get("id")) != key:
            profile = None
        self._snapshots[key] = _Snapshot(self._clock(), profile)
        return profile

    def _note_missing(self, account: AccountRef) -> None:
        """Counts one more check where the account wasn't there; once it has
        been missing in enough checks spaced apart, it's gone for good (the
        worker then marks it invalid). Retries seconds apart count as one."""
        key = account.platform_account_id
        now = self._clock()
        count, last = self._missing.get(key, (0, float("-inf")))
        if now - last >= _STRIKE_SPACING_SECONDS:
            count += 1
            last = now
        self._missing[key] = (count, last)
        if count >= _STRIKES_BEFORE_GONE:
            logger.warning("TikTok @%s has been missing for %d separate checks — marking it invalid.", account.username, count)
            message = (
                f"TikTok account @{account.username} can't be found any more — it may have been deleted, "
                "renamed or made private."
            )
            raise PermanentPlatformError(message, user_message=message)
