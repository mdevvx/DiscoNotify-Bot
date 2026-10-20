"""YouTube Data API v3 adapter.

API availability summary (section 46), verified 2026-09-15 against current
Google developer documentation and community reporting, and re-verified
2026-10-12 against the live API — see [[project-platform-api-policy]] /
README.md for the standing policy this follows:

| Feature                | Supported | Method                                     | Cost/check | Notes |
|-------------------------|-----------|----------------------------------------------|------------|-------|
| New videos              | Yes       | playlistItems.list on the uploads playlist   | 1 unit     | Reliable, cheap. |
| Shorts                  | Heuristic | duration <= 3 min AND vertical/square embed | (included) | No official "is Short" field exists on the API. See parser.py. |
| Premieres               | Yes       | uploads playlist + videos.list               | (included) | Announced as a new video/Short when the Premiere starts, not as a stream. |
| Community posts         | No        | —                                              | —          | No public Data API v3 endpoint exists for the Community tab. |
| Live stream start/end   | Yes       | uploads playlist + videos.list               | 1 unit     | See "Live detection" below. |
| Upcoming/scheduled streams | No     | —                                              | —          | Tracked internally (to catch the moment they go live) but not announced. |

Live detection: a broadcast shows up at the top of the channel's uploads
playlist as soon as it's scheduled or goes live (verified 2026-10-12: every
currently-live stream sampled was at position 0, including one that had
started two minutes earlier; scheduled ones at positions 0-2). So the same
playlistItems.list call that finds new uploads also finds new broadcasts,
and one batched videos.list call refreshes the state of every upcoming/live
broadcast the adapter is tracking. This replaced search.list(eventType=live)
(100 units/call), whose cost forced YouTube's whole scheduler — new-video
detection included — down to one check every ~18 minutes.

Credentials: a plain API key (YOUTUBE_API_KEY) — none of the above needs
OAuth, since it's all public channel/video data.

Quota: 10,000 units/day by default (Settings.youtube_daily_quota_units),
shared across every YouTube channel this bot monitors — it's one budget per
Google Cloud project, not per-account or per-guild. Each channel costs at
most 2 units per check, and the poll interval stretches with the number of
distinct channels so the day's checks fit in the budget
(see poll_interval_seconds()).
"""

from __future__ import annotations

import asyncio
import datetime
import time
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, FrozenSet, List, Optional, Set, Tuple

from events.models import NormalizedEvent
from monitoring.rate_limiter import RateLimiter
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.youtube import parser
from platforms.youtube.client import YouTubeClient
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_LIST_COST = 1
_UPLOADS_PAGE_SIZE = 10
_MAX_IDS_PER_VIDEOS_CALL = 50
# Worst case per channel per check: playlistItems.list + videos.list.
_UNITS_PER_CHECK = 2
# Fraction of the daily quota the polling cadence is sized against. The
# rest is headroom for account-add lookups, uploads-playlist lookups after
# a restart, and the in-memory budget resetting on every restart.
_QUOTA_HEADROOM = 0.9
_ACTIVE_BROADCAST_STATES = frozenset({"upcoming", "live"})
_CURSOR_SEPARATOR = "|"


@dataclass
class _ChannelSnapshot:
    """One check of one channel, shared by every guild monitoring it."""

    fetched_at: float
    # Uploads playlist page, newest first.
    playlist_items: List[Dict[str, Any]]
    # videos.list details for every page item plus any tracked broadcast
    # that has scrolled off the page.
    videos: Dict[str, Dict[str, Any]]
    # Premieres that started (or started and finished) since the previous
    # snapshot — announced as new uploads.
    started_premieres: List[Dict[str, Any]] = field(default_factory=list)


class YouTubeAdapter(PlatformAdapter):
    platform: ClassVar[str] = "youtube"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset(
        {Capability.VIDEOS, Capability.SHORTS, Capability.LIVE_STATUS}
    )
    counts_own_requests: ClassVar[bool] = True

    def __init__(
        self,
        client: YouTubeClient,
        *,
        daily_quota_units: int,
        min_poll_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._daily_quota_units = max(daily_quota_units, _LIST_COST)
        self._budget = RateLimiter(max_requests=self._daily_quota_units, period_seconds=86400)
        self._usable_units_per_day = max(daily_quota_units * _QUOTA_HEADROOM, _UNITS_PER_CHECK)
        self._min_poll_interval = min_poll_interval_seconds
        self._poll_interval = min_poll_interval_seconds
        # Two guilds monitoring the same creator get two account rows, and
        # the scheduler polls both on the same tick. Without sharing one
        # snapshot per *channel*, each would pay for its own API calls.
        self._snapshots: Dict[str, _ChannelSnapshot] = {}
        self._snapshot_locks: Dict[str, asyncio.Lock] = {}
        # Upcoming/live broadcasts per channel, refreshed on every check so
        # the moment one goes live (or ends) is caught even after it has
        # scrolled off the first uploads page.
        self._active_broadcasts: Dict[str, Set[str]] = {}
        # A channel's uploads-playlist ID never changes once known, so
        # caching it avoids spending a channels.list call re-fetching it
        # on every single check. The avatar comes from the same call.
        self._uploads_playlist_cache: Dict[str, str] = {}
        self._channel_avatars: Dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def quota_snapshot(self) -> Optional[Tuple[int, int]]:
        """The bot's own count of units left in its rolling 24 hours. Google
        resets the real quota at midnight Pacific and the count starts from
        zero when the bot restarts, so this is an estimate (it can only
        overstate what's left after a restart)."""
        return self._budget.remaining(), self._daily_quota_units

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        """Fast as the quota allows: every channel gets checked once per
        interval at up to _UNITS_PER_CHECK units, so N channels need
        86400 * N * 2 / budget seconds between checks to last the day. With
        the default 10,000-unit quota that's the 30s floor for 1 channel,
        ~38s for 2, ~58s for 3, ~96s for 5, ~192s for 10."""
        channels = max(distinct_account_count, 1)
        quota_bound = 86400 * channels * _UNITS_PER_CHECK / self._usable_units_per_day
        self._poll_interval = max(self._min_poll_interval, quota_bound)
        return self._poll_interval

    async def validate_account(self, identifier: str) -> AccountRef:
        kind, value = parser.parse_identifier(identifier)
        # Counted against the budget but never blocked by it — an admin
        # adding an account is a one-off, interactive action.
        await self._budget.try_acquire(cost=_LIST_COST)
        channel = await self._client.get_channel(
            channel_id=value if kind == "id" else None,
            handle=value if kind == "handle" else None,
            username=value if kind == "username" else None,
        )
        if channel is None:
            raise PermanentPlatformError(
                f"No YouTube channel found for {identifier!r}",
                user_message="We couldn't find that YouTube channel. Please check the @handle, link or channel ID.",
            )
        return parser.channel_to_account_ref(channel)

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        snapshot = await self._get_snapshot(account)
        if snapshot is None:
            return FetchResult(items=[], next_cursor=cursor)
        if not snapshot.playlist_items:
            # A channel with no uploads yet still needs a baseline — without
            # one, its first upload would be taken as the baseline on the
            # next check and never announced.
            return FetchResult(items=[], next_cursor=cursor or _empty_baseline_cursor())

        next_cursor = _make_cursor(snapshot.playlist_items[0])
        if cursor is None:
            # First-ever poll for this account: establish a baseline
            # without notifying about the channel's entire back-catalog.
            return FetchResult(items=[], next_cursor=next_cursor)

        new_items = _items_after_cursor(snapshot.playlist_items, cursor)
        if new_items is None:
            logger.info(
                "YouTube cursor for %s is no longer on the uploads page and has no timestamp; "
                "re-baselining instead of re-announcing old uploads.",
                account.username,
            )
            return FetchResult(items=[], next_cursor=next_cursor)

        videos = []
        for item in reversed(new_items):  # oldest first, so alerts go out in upload order
            video = snapshot.videos.get(parser.playlist_item_video_id(item))
            # Missing: deleted/privated between the two calls. Broadcasts
            # (streams and Premieres) are excluded here — a stream is
            # get_live_status()'s job, and a Premiere is announced below
            # once it actually starts, not when it's first scheduled.
            if video is not None and not parser.is_live_broadcast(video):
                videos.append(video)
        # Re-reported on every check while the Premiere is playing; the
        # events table's dedup ledger makes that a single notification.
        videos.extend(snapshot.started_premieres)
        return FetchResult(items=videos, next_cursor=next_cursor)

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        snapshot = await self._get_snapshot(account)
        if snapshot is None:
            # Unknown (budget exhausted) — never report "offline", or the
            # worker would announce a stream ended that didn't.
            return None

        live = [video for video in snapshot.videos.values() if parser.is_live_stream(video)]
        if not live:
            return LiveStatus(is_live=False)
        newest = max(live, key=lambda video: video.get("liveStreamingDetails", {}).get("actualStartTime") or "")
        return parser.video_to_live_status(
            newest, channel_avatar_url=self._channel_avatars.get(account.platform_account_id)
        )

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        return parser.normalize_video(
            account, raw_item, channel_avatar_url=self._channel_avatars.get(account.platform_account_id)
        )

    async def _get_snapshot(self, account: AccountRef) -> Optional[_ChannelSnapshot]:
        channel_id = account.platform_account_id
        lock = self._snapshot_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            cached = self._snapshots.get(channel_id)
            # Half the interval: rows for the same channel polled in the
            # same tick share it, while the next tick always refetches.
            if cached is not None and self._clock() - cached.fetched_at < self._poll_interval / 2:
                return cached

            snapshot = await self._fetch_snapshot(account, cached)
            if snapshot is not None:
                self._snapshots[channel_id] = snapshot
            return snapshot

    async def _fetch_snapshot(
        self, account: AccountRef, previous: Optional[_ChannelSnapshot]
    ) -> Optional[_ChannelSnapshot]:
        channel_id = account.platform_account_id
        playlist_id = await self._get_uploads_playlist_id(account)
        if playlist_id is None:
            return None

        if not await self._budget.try_acquire(cost=_LIST_COST):
            self._log_budget_exhausted(account)
            return None
        self._count_request()
        playlist_items = await self._client.list_uploads(playlist_id, max_results=_UPLOADS_PAGE_SIZE)
        page_ids = [parser.playlist_item_video_id(item) for item in playlist_items]

        # A regular upload's details never need refreshing once known; a
        # broadcast's state does (upcoming -> live -> ended).
        previous_videos = previous.videos if previous is not None else {}
        previously_active = self._active_broadcasts.get(channel_id, set())
        videos: Dict[str, Dict[str, Any]] = {
            video_id: previous_videos[video_id]
            for video_id in page_ids
            if video_id in previous_videos and video_id not in previously_active
        }
        to_fetch = _dedupe([video_id for video_id in page_ids if video_id not in videos] + sorted(previously_active))
        to_fetch = to_fetch[:_MAX_IDS_PER_VIDEOS_CALL]

        if to_fetch:
            if not await self._budget.try_acquire(cost=_LIST_COST):
                self._log_budget_exhausted(account)
                return None
            self._count_request()
            for video in await self._client.get_videos(to_fetch):
                videos[video["id"]] = video

        self._active_broadcasts[channel_id] = {
            video_id
            for video_id, video in videos.items()
            if parser.is_live_broadcast(video) and parser.broadcast_state(video) in _ACTIVE_BROADCAST_STATES
        }
        started_premieres = [
            video
            for video in videos.values()
            if parser.is_premiere(video)
            and (
                parser.broadcast_state(video) == "live"
                # Started and finished between two checks.
                or (video["id"] in previously_active and parser.broadcast_state(video) == "none")
            )
        ]
        return _ChannelSnapshot(
            fetched_at=self._clock(),
            playlist_items=playlist_items,
            videos=videos,
            started_premieres=started_premieres,
        )

    async def _get_uploads_playlist_id(self, account: AccountRef) -> Optional[str]:
        cached = self._uploads_playlist_cache.get(account.platform_account_id)
        if cached is not None:
            return cached

        if not await self._budget.try_acquire(cost=_LIST_COST):
            self._log_budget_exhausted(account)
            return None
        self._count_request()
        channel = await self._client.get_channel(channel_id=account.platform_account_id)
        if channel is None:
            raise PermanentPlatformError(f"YouTube channel {account.platform_account_id} no longer exists")

        avatar_url = parser.channel_avatar_url(channel)
        if avatar_url:
            self._channel_avatars[account.platform_account_id] = avatar_url
        playlist_id = parser.uploads_playlist_id(channel)
        if playlist_id:
            self._uploads_playlist_cache[account.platform_account_id] = playlist_id
        return playlist_id

    def _log_budget_exhausted(self, account: AccountRef) -> None:
        logger.warning("Skipping YouTube check for %s — daily quota budget exhausted.", account.username)


def _empty_baseline_cursor() -> str:
    """"|<now>" — no upload yet, so anything added to the playlist after now
    is new. Same timestamp format as the playlist's publishedAt."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{_CURSOR_SEPARATOR}{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _make_cursor(playlist_item: Dict[str, Any]) -> str:
    """"<videoId>|<time added to the playlist>". The timestamp is what lets
    a poll recover when the cursor's video is gone from the page (deleted,
    made private) without either re-announcing the whole page or skipping
    real new uploads."""
    video_id = parser.playlist_item_video_id(playlist_item)
    added_at = playlist_item.get("snippet", {}).get("publishedAt")
    return f"{video_id}{_CURSOR_SEPARATOR}{added_at}" if added_at else video_id


def _parse_cursor(cursor: str) -> Tuple[str, Optional[str]]:
    # Cursors stored before 2026-10-12 are a bare video ID.
    video_id, _, added_at = cursor.partition(_CURSOR_SEPARATOR)
    return video_id, added_at or None


def _items_after_cursor(playlist_items: List[Dict[str, Any]], cursor: str) -> Optional[List[Dict[str, Any]]]:
    """Page items newer than `cursor`, newest first. None when that can't be
    told (cursor gone from the page and carries no timestamp)."""
    cursor_video_id, cursor_added_at = _parse_cursor(cursor)
    newer = []
    for item in playlist_items:
        if parser.playlist_item_video_id(item) == cursor_video_id:
            return newer
        newer.append(item)

    if cursor_added_at is None:
        return None
    # ISO-8601 UTC timestamps in the same format compare correctly as strings.
    return [item for item in playlist_items if (item.get("snippet", {}).get("publishedAt") or "") > cursor_added_at]


def _dedupe(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))
