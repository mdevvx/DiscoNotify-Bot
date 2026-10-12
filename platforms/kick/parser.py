"""Pure functions converting raw Kick Public API JSON into this project's
normalized shapes. No HTTP calls or mutable state here, so this is
trivially testable against fixture JSON without a client or network.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Optional

from platforms.base import AccountRef, LiveStatus

_KICK_URL_RE = re.compile(r"^https?://(www\.)?kick\.com/(?P<slug>[^/?]+)", re.IGNORECASE)


def parse_identifier(identifier: str) -> str:
    """Resolves a user-supplied slug, @handle-style mention, or full
    channel URL to a bare Kick slug — matching KickClient.get_channel_by_slug()'s
    parameter. Kick slugs are always lowercase."""
    value = identifier.strip()

    url_match = _KICK_URL_RE.match(value)
    if url_match:
        value = url_match.group("slug")

    return value.lstrip("@").lower()


def channel_to_account_ref(channel: Dict[str, Any]) -> AccountRef:
    """`channel` is a GET /public/v1/channels resource. The numeric
    broadcaster_user_id (not `slug`) is used as platform_account_id since a
    slug can be renamed but the ID can't — matches how the YouTube/Twitch
    adapters use their platforms' stable channel/user IDs over handles."""
    return AccountRef(
        platform="kick",
        platform_account_id=str(channel["broadcaster_user_id"]),
        username=channel.get("slug") or str(channel["broadcaster_user_id"]),
    )


def livestream_to_live_status(livestream: Dict[str, Any]) -> LiveStatus:
    """`livestream` is one GET /public/v1/livestreams item — present only
    while the broadcaster is live.

    Kick exposes no per-session stream ID, so one is built from the
    broadcaster and the stream's start time: stable for the whole session,
    different for the next one. Without it, every session of a channel
    shares one dedup key and only its first "stream ended" alert ever goes
    out (found 2026-10-12)."""
    slug = livestream.get("slug")
    broadcaster_id = livestream.get("broadcaster_user_id")
    started = livestream.get("started_at")
    category = (livestream.get("category") or {}).get("name")
    return LiveStatus(
        is_live=True,
        stream_id=f"{broadcaster_id}:{started}" if started else None,
        title=livestream.get("stream_title"),
        category=category or None,
        viewer_count=_safe_int(livestream.get("viewer_count")),
        started_at=_parse_timestamp(started),
        thumbnail_url=livestream.get("thumbnail") or None,
        url=f"https://kick.com/{slug}" if slug else None,
        author=slug,
        author_avatar_url=livestream.get("profile_picture") or None,
    )


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
