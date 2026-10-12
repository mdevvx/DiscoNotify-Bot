"""Pure functions converting raw YouTube Data API v3 JSON into this
project's normalized shapes. No HTTP calls or mutable state here, so this
is trivially testable against fixture JSON without a client or network.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Optional, Tuple

from events.models import EventType, NormalizedEvent
from platforms.base import AccountRef, LiveStatus

# No official "is this a Short" field exists on the Data API (verified
# 2026-09-15 against current Google documentation and community reports —
# there is no isShort/shortsEligible field on the Video resource). Shorts
# can be up to 3 minutes (since Oct 2024) and are vertical or square, so the
# heuristic is "<= 3 min AND not landscape", using the embed dimensions
# videos.list returns for part=player when maxHeight is passed (verified
# 2026-10-12 against real uploads: every Short reported 563x1000, landscape
# videos 1778x1000). When the dimensions are missing it falls back to the
# older duration-only "<= 60s" rule. Treat EventType.SHORT_PUBLISHED as
# "probably a Short," not a guarantee.
_SHORT_MAX_SECONDS = 180
_SHORT_MAX_SECONDS_WITHOUT_DIMENSIONS = 60

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_URL_RE = re.compile(r"^https?://(www\.)?youtube\.com/(?P<path>.+)$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_identifier(identifier: str) -> Tuple[str, str]:
    """Resolves a user-supplied channel ID, @handle, legacy username, or
    full URL to (lookup_kind, value) — lookup_kind is 'id', 'handle', or
    'username', matching YouTubeClient.get_channel()'s parameters."""
    value = identifier.strip()

    url_match = _YOUTUBE_URL_RE.match(value)
    if url_match:
        path = url_match.group("path").split("?")[0].strip("/")
        if path.startswith("channel/"):
            return "id", path[len("channel/") :]
        if path.startswith("user/"):
            return "username", path[len("user/") :]
        if path.startswith("c/"):
            path = path[len("c/") :]
        return "handle", path if path.startswith("@") else f"@{path}"

    if _CHANNEL_ID_RE.match(value):
        return "id", value

    return "handle", value if value.startswith("@") else f"@{value}"


def channel_to_account_ref(channel: Dict[str, Any]) -> AccountRef:
    snippet = channel.get("snippet", {})
    return AccountRef(
        platform="youtube",
        platform_account_id=channel["id"],
        username=snippet.get("customUrl") or snippet.get("title") or channel["id"],
    )


def channel_avatar_url(channel: Dict[str, Any]) -> Optional[str]:
    return _best_thumbnail(channel.get("snippet", {}).get("thumbnails"))


def uploads_playlist_id(channel: Dict[str, Any]) -> Optional[str]:
    return channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


def playlist_item_video_id(item: Dict[str, Any]) -> str:
    return item["snippet"]["resourceId"]["videoId"]


def is_live_broadcast(video: Dict[str, Any]) -> bool:
    """A livestream (upcoming, currently live, or already ended) shows up
    in the channel's uploads playlist exactly like a regular upload — the
    only reliable, permanent signal that a video ID is/was a broadcast
    rather than a normal upload is the presence of `liveStreamingDetails`
    on the videos.list resource (unlike `snippet.liveBroadcastContent`,
    which reverts to "none" once the stream ends). fetch_updates() uses
    this to exclude broadcasts from "new video" detection entirely —
    live-transition alerts are get_live_status()'s job; without this
    exclusion, a stream going live (or ending) gets double-reported as a
    "new video" too, using the wrong notification template."""
    return bool(video.get("liveStreamingDetails"))


def broadcast_state(video: Dict[str, Any]) -> str:
    """snippet.liveBroadcastContent: "upcoming", "live", or "none"."""
    return video.get("snippet", {}).get("liveBroadcastContent") or "none"


def is_premiere(video: Dict[str, Any]) -> bool:
    """A Premiere is a pre-recorded upload that plays out like a broadcast,
    so it carries liveStreamingDetails just like a real stream. The
    difference is its duration: a real live/upcoming stream reports
    "P0D" (verified 2026-10-12 against currently-live streams), while a
    Premiere already has the uploaded video's real length."""
    if not is_live_broadcast(video):
        return False
    duration = _parse_iso8601_duration(video.get("contentDetails", {}).get("duration"))
    return bool(duration)


def is_live_stream(video: Dict[str, Any]) -> bool:
    """A real stream (not a Premiere) that is live right now."""
    return broadcast_state(video) == "live" and is_live_broadcast(video) and not is_premiere(video)


def normalize_video(
    account: AccountRef, video: Dict[str, Any], *, channel_avatar_url: Optional[str] = None
) -> NormalizedEvent:
    """`video` is a videos.list resource (part=snippet,contentDetails,player)."""
    snippet = video.get("snippet", {})
    content_details = video.get("contentDetails", {})
    video_id = video["id"]

    duration_seconds = _parse_iso8601_duration(content_details.get("duration"))
    is_probably_short = _is_probably_short(duration_seconds, video.get("player"))

    return NormalizedEvent(
        platform=account.platform,
        platform_account_id=account.platform_account_id,
        account_username=account.username,
        event_type=EventType.SHORT_PUBLISHED if is_probably_short else EventType.VIDEO_PUBLISHED,
        platform_event_id=video_id,
        title=snippet.get("title"),
        description=snippet.get("description"),
        url=f"https://www.youtube.com/watch?v={video_id}",
        thumbnail_url=_best_thumbnail(snippet.get("thumbnails")),
        author=snippet.get("channelTitle"),
        author_avatar_url=channel_avatar_url,
        # A Premiere's publishedAt is when it was scheduled; when it
        # actually started is the moment it was released.
        published_at=_parse_timestamp(
            video.get("liveStreamingDetails", {}).get("actualStartTime") or snippet.get("publishedAt")
        ),
        metadata={"duration_seconds": duration_seconds, "is_short_heuristic": is_probably_short},
    )


def video_to_live_status(video: Dict[str, Any], *, channel_avatar_url: Optional[str] = None) -> LiveStatus:
    """`video` is a videos.list resource (part=snippet,liveStreamingDetails)."""
    snippet = video.get("snippet", {})
    live_details = video.get("liveStreamingDetails", {})

    video_id = video.get("id")
    return LiveStatus(
        is_live=snippet.get("liveBroadcastContent") == "live",
        stream_id=video_id,
        title=snippet.get("title"),
        category=None,  # not exposed for a channel this bot doesn't own, without extra API calls
        viewer_count=_safe_int(live_details.get("concurrentViewers")),
        started_at=_parse_timestamp(live_details.get("actualStartTime")),
        thumbnail_url=_best_thumbnail(snippet.get("thumbnails")),
        url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        description=snippet.get("description"),
        author=snippet.get("channelTitle"),
        author_avatar_url=channel_avatar_url,
    )


def _is_probably_short(duration_seconds: Optional[int], player: Optional[Dict[str, Any]]) -> bool:
    if duration_seconds is None:
        return False
    width = _safe_int((player or {}).get("embedWidth"))
    height = _safe_int((player or {}).get("embedHeight"))
    if not width or not height:
        return duration_seconds <= _SHORT_MAX_SECONDS_WITHOUT_DIMENSIONS
    return duration_seconds <= _SHORT_MAX_SECONDS and width <= height


def _best_thumbnail(thumbnails: Optional[Dict[str, Any]]) -> Optional[str]:
    if not thumbnails:
        return None
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbnails:
            return thumbnails[key].get("url")
    return None


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


def _parse_iso8601_duration(value: Optional[str]) -> Optional[int]:
    """Parses 'PT1M30S'-style durations into total seconds. Covers the
    day/hour/minute/second components YouTube actually emits: "P0D" for a
    live or upcoming stream, and "P1DT2H..." for a video over 24 hours."""
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(group) if group else 0 for group in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
