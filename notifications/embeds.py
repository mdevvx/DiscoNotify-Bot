"""Builds the notification message body from a NormalizedEvent.

Two shapes:

- A rich embed (build_embed) for platforms whose events carry enough data
  to fill one properly — currently YouTube: channel name + avatar, the
  video's real title and description, the full-size thumbnail, and a
  platform footer with the time. The message text then wraps the link in
  <...> so Discord doesn't add its own link preview underneath it.
- Plain text containing the raw link (build_plain_text) for every other
  platform, and whenever an admin turns embeds off — Discord's own link
  preview does the visual work there. A generic hand-built embed with
  little real data looked worse than that preview (2026-09-18), which is
  why build_embed returns None rather than a thin embed for platforms it
  doesn't have the data for.

Pure functions — no Discord API calls, no DB access — so they're testable
against a plain NormalizedEvent without any bot/network setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

import discord

from events.models import EventType, NormalizedEvent

# Field values are capped at 1024 characters; long descriptions (mostly
# link lists and sponsor blurbs) are trimmed well before that so the
# thumbnail stays the focus.
_DESCRIPTION_MAX_LENGTH = 350
_TITLE_MAX_LENGTH = 256


@dataclass(frozen=True)
class _EmbedStyle:
    color: int
    footer_icon_url: str
    # Per event type: (footer label, embed headline template).
    events: Dict[EventType, "tuple[str, str]"]


_YOUTUBE_ICON_URL = "https://www.gstatic.com/youtube/img/branding/favicon/favicon_144x144.png"

_EMBED_STYLES: Dict[str, _EmbedStyle] = {
    "youtube": _EmbedStyle(
        color=0xFF0000,
        footer_icon_url=_YOUTUBE_ICON_URL,
        events={
            EventType.STREAM_STARTED: ("YouTube Live", "{name} is now live on YouTube!"),
            EventType.VIDEO_PUBLISHED: ("YouTube", "{name} uploaded a new video on YouTube!"),
            EventType.SHORT_PUBLISHED: ("YouTube Shorts", "{name} posted a new Short on YouTube!"),
        },
    ),
}


def build_embed(event: NormalizedEvent) -> Optional[discord.Embed]:
    """None when this platform/event type has no rich embed — send
    build_plain_text() instead."""
    style = _EMBED_STYLES.get(event.platform)
    if style is None or event.event_type not in style.events or not event.url:
        return None
    footer_label, headline = style.events[event.event_type]
    name = display_name(event)

    embed = discord.Embed(
        title=_truncate(event.title or footer_label, _TITLE_MAX_LENGTH),
        url=event.url,
        description=headline.format(name=name),
        color=discord.Color(style.color),
        timestamp=event.started_at or event.published_at,
    )
    embed.set_author(name=name, icon_url=event.author_avatar_url, url=_account_url(event))
    description = (event.description or "").strip()
    if description:
        embed.add_field(name="Description", value=_truncate(description, _DESCRIPTION_MAX_LENGTH), inline=False)
    if event.author_avatar_url:
        embed.set_thumbnail(url=event.author_avatar_url)
    if event.thumbnail_url:
        embed.set_image(url=event.thumbnail_url)
    embed.set_footer(text=footer_label, icon_url=style.footer_icon_url)
    return embed


_EMBED_HEADLINES: Dict[EventType, str] = {
    EventType.STREAM_STARTED: "🔴 **{name}** is live now! {url}",
    EventType.VIDEO_PUBLISHED: "🆕 **{name}** uploaded a new video! {url}",
    EventType.SHORT_PUBLISHED: "🆕 **{name}** posted a new Short! {url}",
}


def build_embed_headline(event: NormalizedEvent) -> str:
    """The one-line message text above build_embed()'s embed (which already
    carries the title and description). The link is wrapped in <...> —
    still clickable, but Discord won't add its own preview below the embed."""
    template = _EMBED_HEADLINES.get(event.event_type, "🆕 **{name}** has an update! {url}")
    return template.format(
        name=_safe(discord.utils.escape_markdown(display_name(event))),
        url=f"<{event.url}>" if event.url else "",
    ).strip()


def build_plain_text(event: NormalizedEvent) -> str:
    builder = _TEXT_BUILDERS.get(event.event_type, _text_generic)
    return builder(event, event.url)


def display_name(event: NormalizedEvent) -> str:
    """The platform's display name (a YouTube channel's title) when known,
    else the handle the account was added with."""
    return event.author or event.account_username


def _account_url(event: NormalizedEvent) -> Optional[str]:
    if event.platform == "youtube":
        return f"https://www.youtube.com/channel/{event.platform_account_id}"
    return None


def _safe(text: str) -> str:
    """Platform-sourced text going into the message *content* (where
    mentions are live) must never be able to ping — a video titled
    "@everyone ..." would otherwise make the sender allow that ping. Embeds
    never ping, so embed fields don't need this."""
    return discord.utils.escape_mentions(text)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _text_video(event: NormalizedEvent, url: Optional[str]) -> str:
    return _join(f"🆕 **{_safe(event.account_username)}** published a new video: {_safe(event.title or '')}".strip(), url)


def _text_short(event: NormalizedEvent, url: Optional[str]) -> str:
    return _join(f"🆕 **{_safe(event.account_username)}** published a new Short: {_safe(event.title or '')}".strip(), url)


def _text_post(event: NormalizedEvent, url: Optional[str]) -> str:
    return _join(f"🆕 **{_safe(event.account_username)}** posted: {_safe(event.title or event.description or '')}".strip(), url)


def _text_stream_started(event: NormalizedEvent, url: Optional[str]) -> str:
    return _join(f"🔴 **{_safe(event.account_username)}** is now live! {_safe(event.title or '')}".strip(), url)


def _text_stream_ended(event: NormalizedEvent, url: Optional[str]) -> str:
    return f"⚫ **{_safe(event.account_username)}**'s stream has ended."


def _text_generic(event: NormalizedEvent, url: Optional[str]) -> str:
    return _join(f"🆕 **{_safe(event.account_username)}** has an update: {_safe(event.title or '')}".strip(), url)


def _join(text: str, url: Optional[str]) -> str:
    return f"{text}\n{url}" if url else text


_TEXT_BUILDERS: Dict[EventType, Callable[[NormalizedEvent, Optional[str]], str]] = {
    EventType.VIDEO_PUBLISHED: _text_video,
    EventType.SHORT_PUBLISHED: _text_short,
    EventType.POST_CREATED: _text_post,
    EventType.STREAM_STARTED: _text_stream_started,
    EventType.STREAM_ENDED: _text_stream_ended,
}
