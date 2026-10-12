"""Pure functions converting raw Instagram Graph API (Business Discovery)
JSON into this project's normalized shapes. No HTTP calls or mutable
state here, so this is trivially testable against fixture JSON without a
client or network.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Optional

from events.models import EventType, NormalizedEvent
from platforms.base import AccountRef

_INSTAGRAM_URL_RE = re.compile(r"^https?://(www\.)?instagram\.com/(?P<username>[^/?]+)", re.IGNORECASE)


def parse_identifier(identifier: str) -> str:
    """Resolves a user-supplied username, @mention, or full profile URL to
    a bare username — matching InstagramClient.get_business_discovery()'s
    parameter. Instagram usernames are always lowercase."""
    value = identifier.strip()

    url_match = _INSTAGRAM_URL_RE.match(value)
    if url_match:
        value = url_match.group("username")

    return value.lstrip("@").lower()


def discovery_to_account_ref(discovery: Dict[str, Any]) -> AccountRef:
    """`discovery` is a business_discovery resource. Unlike the other
    adapters, `username` here isn't just for display — Business Discovery
    can only be queried by username (there's no by-ID lookup), so every
    subsequent fetch_updates() call re-supplies account.username as the
    live query key. If the target ever renames their handle, polling
    breaks until the account is removed and re-added — a real, documented
    limitation of this endpoint, not something this adapter can work
    around."""
    return AccountRef(
        platform="instagram",
        platform_account_id=discovery["id"],
        username=discovery.get("username") or discovery["id"],
    )


def normalize_media(account: AccountRef, media: Dict[str, Any]) -> NormalizedEvent:
    """`media` is one item from business_discovery's `media` edge, with the
    account's profile (name, profile picture) attached by the adapter under
    "_profile". Reels are told apart from feed posts by media_product_type
    ("REELS" vs "FEED" — verified against the live API 2026-10-12); both are
    POST_CREATED events, with `is_reel` in metadata for the alert wording."""
    media_id = media["id"]
    profile = media.get("_profile") or {}
    is_video = media.get("media_type") == "VIDEO"
    return NormalizedEvent(
        platform=account.platform,
        platform_account_id=account.platform_account_id,
        account_username=account.username,
        event_type=EventType.POST_CREATED,
        platform_event_id=media_id,
        description=media.get("caption"),
        url=media.get("permalink"),
        # A video's media_url is the video file itself; its still image is
        # thumbnail_url.
        thumbnail_url=media.get("thumbnail_url") if is_video else media.get("media_url"),
        author=profile.get("name") or account.username,
        author_avatar_url=profile.get("profile_picture_url"),
        published_at=_parse_timestamp(media.get("timestamp")),
        metadata={
            "media_type": media.get("media_type"),
            "media_product_type": media.get("media_product_type"),
            "is_reel": media.get("media_product_type") == "REELS",
        },
    )


def _parse_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        # Meta's "+0000" offset isn't ISO 8601 enough for Python < 3.11.
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
