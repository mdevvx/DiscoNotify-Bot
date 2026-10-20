"""Pure functions converting TikTok's public creator-embed data (see
client.py) into this project's normalized shapes. No HTTP calls or mutable
state.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

from events.models import EventType, NormalizedEvent
from platforms.base import AccountRef
from utils.errors import PermanentPlatformError

_TIKTOK_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?tiktok\.com/@(?P<handle>[^/?#]+)", re.IGNORECASE)
# 2–24 letters, numbers, underscores and periods; a name can't end in a period.
_HANDLE_RE = re.compile(r"^(?=.{2,24}$)[A-Za-z0-9_.]*[A-Za-z0-9_]$")


def parse_identifier(identifier: str) -> str:
    """A handle, @handle or tiktok.com profile / video URL → the bare handle.
    Raises PermanentPlatformError for anything that can't be one."""
    value = identifier.strip()
    url_match = _TIKTOK_URL_RE.match(value)
    if url_match:
        value = url_match.group("handle")
    elif "tiktok.com" in value.lower():
        # vm.tiktok.com/… and tiktok.com/t/… links name a video, not a profile.
        message = (
            "That's a TikTok short link, which doesn't show whose account it is. "
            "Open it and use the profile link (tiktok.com/@name) or just the @name."
        )
        raise PermanentPlatformError(message, user_message=message)
    value = value.lstrip("@")
    if not _HANDLE_RE.match(value):
        message = "That doesn't look like a TikTok username. Use the @name or the profile link (tiktok.com/@name)."
        raise PermanentPlatformError(message, user_message=message)
    return value


def user_to_account_ref(user: Dict[str, Any]) -> AccountRef:
    """The numeric ID is stored as the account's identity; `username` holds
    the @name, which is what the embed page is fetched by."""
    return AccountRef(platform="tiktok", platform_account_id=str(user["id"]), username=str(user["uniqueId"]))


def public_videos(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The account's public videos, oldest first. IDs grow with time, so
    sorting by ID is sorting by upload time (and keeps a pinned old video
    from looking like the newest)."""
    kept = [video for video in videos if video.get("id") and not video.get("privateItem")]
    return sorted(kept, key=lambda video: int(video["id"]))


def newest_id(videos: List[Dict[str, Any]]) -> Optional[int]:
    return max((int(video["id"]) for video in videos if video.get("id")), default=None)


def video_published_at(video_id: str) -> Optional[datetime.datetime]:
    """A TikTok video ID starts with its creation time in Unix seconds
    (the top 32 bits)."""
    try:
        return datetime.datetime.fromtimestamp(int(video_id) >> 32, tz=datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def normalize_video(account: AccountRef, item: Dict[str, Any]) -> NormalizedEvent:
    """`item` is {"video": <embed video>, "user": <embed userInfo>}."""
    video = item["video"]
    user = item.get("user") or {}
    video_id = str(video["id"])
    handle = user.get("uniqueId") or video.get("authorUniqueId") or account.username
    return NormalizedEvent(
        platform=account.platform,
        platform_account_id=account.platform_account_id,
        account_username=handle,
        event_type=EventType.VIDEO_PUBLISHED,
        platform_event_id=video_id,
        description=(video.get("desc") or "").strip() or None,
        url=f"https://www.tiktok.com/@{handle}/video/{video_id}",
        thumbnail_url=video.get("originCoverUrl") or video.get("coverUrl"),
        author=user.get("nickname") or handle,
        author_avatar_url=user.get("avatarThumbUrl"),
        published_at=video_published_at(video_id),
        metadata={"play_count": video.get("playCount")},
    )
