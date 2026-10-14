"""Pure functions converting X's embedded-timeline posts (see client.py)
into this project's normalized shapes. No HTTP calls or mutable state.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

from events.models import EventType, NormalizedEvent
from platforms.base import AccountRef
from utils.errors import PermanentPlatformError

_X_URL_RE = re.compile(r"^https?://(www\.|mobile\.)?(x|twitter)\.com/(?P<handle>[^/?#]+)", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# X's own profile paths, never a user's handle.
_RESERVED = {"home", "explore", "search", "i", "intent", "settings", "notifications", "messages", "share"}
_CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"  # "Wed Oct 14 15:16:26 +0000 2026"


def parse_identifier(identifier: str) -> str:
    """A handle, @handle or x.com / twitter.com profile URL → the bare
    handle. Raises PermanentPlatformError for anything that can't be one."""
    value = identifier.strip()
    url_match = _X_URL_RE.match(value)
    if url_match:
        value = url_match.group("handle")
    value = value.lstrip("@")
    if not _HANDLE_RE.match(value) or value.lower() in _RESERVED:
        raise PermanentPlatformError(
            f"{identifier!r} isn't an X handle — use the @handle (letters, numbers, _; up to 15) or the profile URL."
        )
    return value


def user_to_account_ref(user: Dict[str, Any]) -> AccountRef:
    """The numeric ID is stored as the account's identity; `username` holds
    the @handle, which is what the timeline is fetched by."""
    return AccountRef(platform="twitter", platform_account_id=str(user["id_str"]), username=user["screen_name"])


def original_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The account's own new posts — retweets and replies left out (quote
    posts kept) — oldest first."""
    kept = [post for post in posts if not post.get("retweeted_status") and not post.get("in_reply_to_status_id_str")]
    return sorted(kept, key=lambda post: int(post["id_str"]))


def normalize_tweet(account: AccountRef, tweet: Dict[str, Any]) -> NormalizedEvent:
    tweet_id = str(tweet["id_str"])
    user = tweet.get("user") or {}
    handle = user.get("screen_name") or account.username
    media = (tweet.get("extended_entities") or tweet.get("entities") or {}).get("media") or []
    first_media = media[0] if media else {}
    return NormalizedEvent(
        platform=account.platform,
        platform_account_id=account.platform_account_id,
        account_username=handle,
        event_type=EventType.POST_CREATED,
        platform_event_id=tweet_id,
        description=_readable_text(tweet) or None,
        url=f"https://x.com/{handle}/status/{tweet_id}",
        # A photo's media_url_https is the image; for a video or GIF it's
        # the preview frame.
        thumbnail_url=first_media.get("media_url_https"),
        author=user.get("name") or handle,
        author_avatar_url=_full_size_avatar(user.get("profile_image_url_https")),
        published_at=_parse_created_at(tweet.get("created_at")),
        metadata={
            "is_quote": bool(tweet.get("is_quote_status")),
            "media_type": first_media.get("type"),
            "media_count": len(media),
        },
    )


def _readable_text(tweet: Dict[str, Any]) -> str:
    """The post's text with t.co links swapped for the real URLs, and the
    trailing link to its own photo/video removed (the card shows it)."""
    text = tweet.get("full_text") or tweet.get("text") or ""
    entities = tweet.get("entities") or {}
    for link in entities.get("urls") or []:
        if link.get("url") and link.get("expanded_url"):
            text = text.replace(link["url"], link["expanded_url"])
    for item in (tweet.get("extended_entities") or entities).get("media") or []:
        if item.get("url"):
            text = text.replace(item["url"], "")
    return text.strip()


def _full_size_avatar(url: Optional[str]) -> Optional[str]:
    """X gives a 48×48 "_normal" avatar; "_400x400" is the same image at a
    size that looks right in a card."""
    return url.replace("_normal.", "_400x400.") if url else None


def _parse_created_at(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, _CREATED_AT_FORMAT)
    except ValueError:
        return None
