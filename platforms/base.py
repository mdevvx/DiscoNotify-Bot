"""The interface every platform adapter (YouTube, Twitch, Kick, ...) implements.

Nothing in monitoring/ ever imports a concrete platform's client or parser
directly — it only ever talks to a PlatformAdapter. That's what lets a new
platform be added by writing one adapter and registering it, without
touching the scheduler, worker, or event pipeline (section 26).

platforms/youtube/ is the first (and so far only) concrete implementation
of this contract.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from events.models import EventType, NormalizedEvent


class Capability(enum.Enum):
    """What a platform adapter can actually do — section 3's capability list.

    Not every platform supports every capability (e.g. Kick has no public
    API for post-style content), so the framework checks `adapter.supports(...)`
    before calling the corresponding method rather than assuming every
    adapter does everything.
    """

    POSTS = "posts"
    VIDEOS = "videos"
    SHORTS = "shorts"
    STREAMS = "streams"
    LIVE_STATUS = "live_status"
    CHANNEL_UPDATES = "channel_updates"


# Capabilities that flow through fetch_updates()/normalize_event() — if an
# adapter declares any of these, the worker polls its "new content" path.
CONTENT_CAPABILITIES: FrozenSet[Capability] = frozenset(
    {Capability.POSTS, Capability.VIDEOS, Capability.SHORTS, Capability.CHANNEL_UPDATES}
)

# What alert_configurations rows to seed when an account is added — one per
# EventType a supported capability could actually produce. Shared here
# (rather than duplicated per platform command) since every adapter uses
# the same capability set and event-type domain.
_CAPABILITY_EVENT_TYPES: Dict[Capability, FrozenSet[EventType]] = {
    Capability.POSTS: frozenset({EventType.POST_CREATED}),
    Capability.VIDEOS: frozenset({EventType.VIDEO_PUBLISHED}),
    Capability.SHORTS: frozenset({EventType.SHORT_PUBLISHED}),
    Capability.STREAMS: frozenset({EventType.STREAM_STARTED, EventType.STREAM_ENDED, EventType.STREAM_UPDATED}),
    Capability.LIVE_STATUS: frozenset({EventType.STREAM_STARTED, EventType.STREAM_ENDED}),
    Capability.CHANNEL_UPDATES: frozenset({EventType.ACCOUNT_UPDATED}),
}


def event_types_for_capabilities(capabilities: FrozenSet[Capability]) -> FrozenSet[EventType]:
    result: set = set()
    for capability in capabilities:
        result.update(_CAPABILITY_EVENT_TYPES.get(capability, ()))
    return frozenset(result)


@dataclass(frozen=True)
class AccountRef:
    """Identifies one account to a platform adapter — not a database row.

    Two different guilds monitoring the same YouTube channel each get their
    own AccountRef with the same platform_account_id; this type carries no
    guild/database identity on purpose (see events/models.py's docstring).
    """

    platform: str
    platform_account_id: str
    username: str


@dataclass(frozen=True)
class LiveStatus:
    """A point-in-time snapshot from get_live_status(). Not itself an event —

    monitoring.worker compares this against the persisted live_states row to
    decide whether a STREAM_STARTED/STREAM_ENDED transition actually
    happened, so the same "still live" snapshot polled repeatedly never
    produces repeat notifications (section 8).
    """

    is_live: bool
    stream_id: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    viewer_count: Optional[int] = None
    started_at: Optional[Any] = None  # datetime; Any avoids importing datetime just for this
    thumbnail_url: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    # The account's display name and avatar as the platform shows them
    # (e.g. a YouTube channel's title, not its @handle) — used by
    # notification embeds where the platform provides them.
    author: Optional[str] = None
    author_avatar_url: Optional[str] = None


@dataclass(frozen=True)
class FetchResult:
    """Raw items newer than `cursor`, plus the cursor to pass in next time.

    `items` are adapter-defined (whatever shape the platform's API returns)
    — normalize_event() is what turns each one into a NormalizedEvent.
    `next_cursor` is opaque outside the adapter; the framework only ever
    stores and replays it, never inspects it.
    """

    items: List[Any]
    next_cursor: Optional[str]


class PlatformAdapter(abc.ABC):
    """Base class every concrete platform adapter extends."""

    platform: ClassVar[str]
    capabilities: ClassVar[FrozenSet[Capability]]

    # Adapters that serve several accounts from one shared/cached API call
    # (YouTube, Kick, Instagram) count the calls they really make, so the
    # health stats show actual usage rather than one request per account
    # per tick. The worker skips its own per-call counting for them.
    counts_own_requests: ClassVar[bool] = False

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def _count_request(self, count: int = 1) -> None:
        self._request_count = getattr(self, "_request_count", 0) + count

    def take_request_count(self) -> int:
        """Real API calls made since the last call to this (then resets)."""
        count = getattr(self, "_request_count", 0)
        self._request_count = 0
        return count

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        """The scheduler cadence this adapter wants, given how many distinct
        platform accounts (not guild-scoped rows) are being monitored.
        Re-asked every cycle, so it follows accounts being added/removed.

        None (the default) means MonitoringManager's generic per-capability
        interval from Settings applies — fine for a platform whose checks
        are cheap (Twitch, Kick). An adapter with a daily quota (YouTube)
        overrides this to poll as fast as that quota can sustain, so it
        neither wastes latency nor burns the whole budget early in the day
        and goes dark for the rest of it."""
        return None

    def quota_snapshot(self) -> Optional[Tuple[int, int]]:
        """(remaining, limit) of the quota this adapter spends, when the
        platform has one and its current value is known — written to
        platform_health once per scheduler cycle. None (the default) for a
        platform with no quota (Twitch, Kick, X, TikTok): nothing is
        fabricated for those."""
        return None

    @abc.abstractmethod
    async def validate_account(self, identifier: str) -> AccountRef:
        """Resolve a user-supplied identifier (URL, handle, or ID) to a real account.

        Raises PermanentPlatformError (utils.errors) if the identifier
        doesn't resolve to an account — that's a "tell the user, don't
        retry" failure, not a transient one.
        """

    @abc.abstractmethod
    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        """Fetch content newer than `cursor`. Only called if the adapter
        declares at least one of the CONTENT_CAPABILITIES."""

    @abc.abstractmethod
    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        """Current live/offline status. Only called if the adapter declares
        Capability.LIVE_STATUS; adapters that don't support it may raise
        NotImplementedError since the worker will never call it."""

    @abc.abstractmethod
    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        """Convert one raw item from fetch_updates() into a NormalizedEvent.

        Synchronous and side-effect free by design — this is pure data
        transformation, not another API call.
        """

    async def check_credentials(self) -> None:
        """Called once at startup. Logs a warning if the adapter's
        credentials are invalid or about to expire; must never raise — a
        bad credential shouldn't stop the bot or the other platforms.
        Default no-op."""

    async def aclose(self) -> None:
        """Releases any resources the adapter owns (e.g. an HTTP client
        session). Default no-op — override only if there's something to
        close. Called by MonitoringManager.stop() during shutdown."""
