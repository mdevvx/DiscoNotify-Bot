"""Kick Public API adapter — live/offline detection.

API research first done 2026-09-15 against Kick's official developer
documentation (docs.kick.com), re-verified against the live API on
2026-10-12 — see [[project-platform-api-policy]] / README.md for the
standing policy this follows:

| Feature          | Method                                                  | Auth             | Notes |
|------------------|---------------------------------------------------------|------------------|-------|
| Resolve a name   | GET /public/v1/channels?slug=                           | app access token | Unknown slug → HTTP 400 "Invalid request" |
| Live/offline     | GET /public/v1/livestreams?broadcaster_user_id=… (≤50)  | app access token | Only live broadcasters are returned; includes title, thumbnail, start time, category, viewers, profile picture |

Every account is checked with ONE batched /livestreams call per tick, no
matter how many accounts are monitored: the first account polled in a tick
fetches all known broadcasters at once and the rest are served from that
snapshot. Kick documents no rate limit and sends no rate-limit headers, so
the cadence is a fixed 30s (KICK_POLL_INTERVAL_SECONDS).

Kick's OAuth (id.kick.com/oauth/token) supports a client_credentials grant
producing an "App Access Token" documented as usable "when user login is
not required" for public data — so no per-streamer authorization.

No webhook: Kick's webhook system needs a public HTTPS callback endpoint
this project doesn't have, and batched polling is cheap.

Kick exposes no per-session stream ID; platforms/kick/parser.py builds one
from the broadcaster and the stream's start time.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, FrozenSet, Optional, Set

from events.models import NormalizedEvent
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.kick import parser
from platforms.kick.client import MAX_IDS_PER_CALL, KickClient
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _Snapshot:
    fetched_at: float
    queried: Set[str] = field(default_factory=set)
    live: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class KickAdapter(PlatformAdapter):
    platform: ClassVar[str] = "kick"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.LIVE_STATUS})
    counts_own_requests: ClassVar[bool] = True

    def __init__(
        self,
        client: KickClient,
        *,
        poll_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        # Every broadcaster any guild has asked about, so one call covers
        # them all.
        self._known_ids: Set[str] = set()
        self._snapshot: Optional[_Snapshot] = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        # One batched call per tick regardless of the account count (up to
        # 50 per call — more than that costs one extra call per 50).
        return self._poll_interval

    async def validate_account(self, identifier: str) -> AccountRef:
        slug = parser.parse_identifier(identifier)
        channel = await self._client.get_channel_by_slug(slug) if slug else None
        if channel is None:
            raise PermanentPlatformError(f"no Kick channel named {slug or identifier!r}")
        return parser.channel_to_account_ref(channel)

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        raise NotImplementedError("KickAdapter declares no content capability — the worker never calls this.")

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        broadcaster_id = account.platform_account_id
        self._known_ids.add(broadcaster_id)
        snapshot = await self._get_snapshot(broadcaster_id)
        livestream = snapshot.live.get(broadcaster_id)
        if livestream is None:
            return LiveStatus(is_live=False)
        return parser.livestream_to_live_status(livestream)

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        raise NotImplementedError("KickAdapter declares no content capability — the worker never calls this.")

    async def _get_snapshot(self, broadcaster_id: str) -> _Snapshot:
        async with self._lock:
            snapshot = self._snapshot
            fresh = snapshot is not None and self._clock() - snapshot.fetched_at < self._poll_interval / 2
            if fresh and broadcaster_id in snapshot.queried:  # type: ignore[union-attr]
                return snapshot  # type: ignore[return-value]

            ids = sorted(self._known_ids)
            live: Dict[str, Dict[str, Any]] = {}
            for start in range(0, len(ids), MAX_IDS_PER_CALL):
                self._count_request()
                for item in await self._client.get_livestreams(ids[start : start + MAX_IDS_PER_CALL]):
                    live[str(item.get("broadcaster_user_id"))] = item
            self._snapshot = _Snapshot(fetched_at=self._clock(), queried=set(ids), live=live)
            return self._snapshot
