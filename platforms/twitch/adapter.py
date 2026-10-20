"""Twitch Helix API adapter — live/offline detection (Phase 5).

API research (2026-09-15), verified against Twitch's current developer
documentation — see
[[project-platform-api-policy]] / README.md for the standing policy this
follows:

| Feature             | Method                        | Auth              | Notes |
|----------------------|-------------------------------|-------------------|-------|
| Live/offline         | GET /helix/streams?user_id=   | app access token  | No broadcaster authorization needed — this event requires no scope. Batches up to 100 IDs/call, but (like the YouTube adapter) this one polls one account at a time to match the framework's per-account worker model; Twitch's 800-points/minute app-token budget makes cross-account batching unnecessary at this bot's scale. |
| Channel resolution   | GET /helix/users?login=       | app access token  | Resolves a login/URL to a stable numeric user ID, stored as platform_account_id — a login can be renamed, the ID can't. |

Considered and explicitly ruled out (2026-09-15, user sign-off) — see
README's Monitoring framework section for the full writeup:
- **EventSub WebSocket transport** — needs a *user* access token capped at
  a total subscription cost of 10 per (client_id, user) tuple. Not viable
  for monitoring arbitrary streamers who haven't authorized this app.
- **EventSub webhooks** — the "correct" push-based approach (instant
  alerts, a 10,000 cost ceiling per client_id), but requires standing up a
  public HTTPS callback endpoint (valid TLS, port 443) this project
  doesn't have yet. Deferred until that infrastructure exists — polling is
  cheap enough here (unlike YouTube's live-search quota crisis) that
  there's no cost pressure forcing the issue.

Credentials: TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET, an app access token via
the OAuth client-credentials flow — no per-broadcaster authorization.

No adapter-level quota budgeting is needed here, unlike YouTube: Twitch's
app-token bucket is 800 points/minute and this adapter spends 1 point per
account per poll — the framework's own generic per-platform RateLimiter
(MONITORING_RATE_LIMIT_REQUESTS/_PERIOD_SECONDS, default 30/60s) is
already far more conservative than that budget.

VOD/clip publishing and channel title/category updates while already live
(STREAM_UPDATED) aren't implemented this phase — Twitch's core value here
is live-alerting, matching what the phase was scoped for.
"""

from __future__ import annotations

from typing import Any, ClassVar, FrozenSet, Optional

from events.models import NormalizedEvent
from platforms.base import AccountRef, Capability, FetchResult, LiveStatus, PlatformAdapter
from platforms.twitch import parser
from platforms.twitch.client import TwitchClient
from utils.errors import PermanentPlatformError


class TwitchAdapter(PlatformAdapter):
    platform: ClassVar[str] = "twitch"
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.LIVE_STATUS})

    def __init__(self, client: TwitchClient) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def validate_account(self, identifier: str) -> AccountRef:
        login = parser.parse_identifier(identifier)
        user = await self._client.get_user(login=login)
        if user is None:
            raise PermanentPlatformError(
                f"No Twitch channel found for {identifier!r}",
                user_message="We couldn't find that Twitch channel. Please check the channel name or link.",
            )
        return parser.user_to_account_ref(user)

    async def fetch_updates(self, account: AccountRef, cursor: Optional[str]) -> FetchResult:
        raise NotImplementedError("TwitchAdapter declares no content capability — the worker never calls this.")

    async def get_live_status(self, account: AccountRef) -> Optional[LiveStatus]:
        stream = await self._client.get_stream(account.platform_account_id)
        if stream is None:
            return LiveStatus(is_live=False)
        return parser.stream_to_live_status(stream)

    def normalize_event(self, account: AccountRef, raw_item: Any) -> NormalizedEvent:
        raise NotImplementedError("TwitchAdapter declares no content capability — the worker never calls this.")
