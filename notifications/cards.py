"""Builds alert messages as Components V2 layouts (discord.ui.LayoutView).

Shape of every alert:

- One text line above the card — the admin's custom message, or the
  default headline ("🔴 **Vector Games** is live now!") with the mention
  role in front. This is the only place a mention is meant to ping.
- An accent-coloured container: channel name + avatar, the linked title, a
  "<name> is now live on YouTube!" line, the description, the full-size
  thumbnail, a link button, and a small footer with the platform and time.
  Each piece only appears when the platform provided it.

With embeds turned off for an alert type (`embed_enabled`), only the text
line is sent, with the link on its own line. A V2 message never gets
Discord's own link preview, so the container is the only preview there is.

Mentions inside V2 text *do* ping (unlike in legacy embeds), so every
piece of platform-sourced text — titles, descriptions, channel names — is
mention-escaped before it goes in. Otherwise a video titled "@everyone"
would ping the server whenever its alerts allow @everyone.

Pure functions — no Discord API calls, no DB access.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord

from events.models import EventType, NormalizedEvent

# Long descriptions (mostly link lists and sponsor blurbs) are trimmed so
# the thumbnail stays the focus; a V2 message allows 4000 text characters.
_DESCRIPTION_MAX_LENGTH = 350
_TITLE_MAX_LENGTH = 256
_DEFAULT_COLOR = 0x5865F2


@dataclass(frozen=True)
class _PlatformStyle:
    name: str
    color: int
    button_label: str
    # Footer label per event type; anything missing uses `name`.
    footers: Dict[EventType, str]
    description_label: str = "Description"


_STYLES: Dict[str, _PlatformStyle] = {
    "youtube": _PlatformStyle(
        name="YouTube",
        color=0xFF0000,
        button_label="Watch on YouTube",
        footers={
            EventType.STREAM_STARTED: "YouTube Live",
            EventType.SHORT_PUBLISHED: "YouTube Shorts",
        },
    ),
    "kick": _PlatformStyle(name="Kick", color=0x53FC18, button_label="Watch on Kick", footers={}),
    "instagram": _PlatformStyle(
        name="Instagram", color=0xE1306C, button_label="Open on Instagram", footers={}, description_label="Caption"
    ),
}

_HEADLINES: Dict[EventType, str] = {
    EventType.STREAM_STARTED: "🔴 **{name}** is live now!",
    EventType.VIDEO_PUBLISHED: "🆕 **{name}** uploaded a new video!",
    EventType.SHORT_PUBLISHED: "🆕 **{name}** posted a new Short!",
    EventType.POST_CREATED: "🆕 **{name}** shared a new post!",
    EventType.STREAM_ENDED: "⚫ **{name}**'s stream has ended.",
}

_BLURBS: Dict[EventType, str] = {
    EventType.STREAM_STARTED: "{name} is now live on {platform}!",
    EventType.VIDEO_PUBLISHED: "{name} uploaded a new video on {platform}!",
    EventType.SHORT_PUBLISHED: "{name} posted a new Short on {platform}!",
    EventType.POST_CREATED: "{name} shared a new post on {platform}!",
    EventType.STREAM_ENDED: "{name}'s stream on {platform} has ended.",
}


_REEL_HEADLINE = "🎬 **{name}** posted a new Reel!"
_REEL_BLURB = "{name} posted a new Reel on {platform}!"


def build_alert(
    event: NormalizedEvent,
    *,
    top_line: str,
    rich: bool,
) -> discord.ui.LayoutView:
    """`top_line` is the text above the card (headline or custom message,
    with any mention already in it). `rich=False` sends only that line."""
    view = discord.ui.LayoutView(timeout=None)
    if not rich:
        view.add_item(discord.ui.TextDisplay(top_line))
        return view
    view.add_item(discord.ui.TextDisplay(top_line))
    view.add_item(build_card(event))
    return view


def build_card(event: NormalizedEvent) -> discord.ui.Container:
    style = _style(event)
    name = _safe(display_name(event))
    header: List[discord.ui.TextDisplay] = []

    account_url = _account_url(event)
    header.append(discord.ui.TextDisplay(f"**[{_link_text(name)}]({account_url})**" if account_url else f"**{name}**"))
    if event.title:
        title = _link_text(_safe(_truncate(event.title, _TITLE_MAX_LENGTH)))
        header.append(discord.ui.TextDisplay(f"### [{title}]({event.url})" if event.url else f"### {title}"))
    blurb = _REEL_BLURB if _is_reel(event) else _BLURBS.get(event.event_type, "{name} has an update on {platform}.")
    header.append(discord.ui.TextDisplay(blurb.format(name=name, platform=style.name)))

    children: list = []
    if event.author_avatar_url:
        children.append(discord.ui.Section(*header, accessory=discord.ui.Thumbnail(event.author_avatar_url)))
    else:
        children.extend(header)

    details = _stream_details(event)
    if details:
        children.append(discord.ui.TextDisplay(details))

    description = (event.description or "").strip()
    if description and event.event_type != EventType.STREAM_ENDED:
        children.append(
            discord.ui.TextDisplay(
                f"**{style.description_label}**\n{_safe(_truncate(description, _DESCRIPTION_MAX_LENGTH))}"
            )
        )
    if event.thumbnail_url and event.event_type != EventType.STREAM_ENDED:
        children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(event.thumbnail_url)))
    if event.url and event.event_type != EventType.STREAM_ENDED:
        children.append(
            discord.ui.ActionRow(
                discord.ui.Button(style=discord.ButtonStyle.link, label=style.button_label, url=event.url)
            )
        )
    children.append(discord.ui.TextDisplay(f"-# {_footer(event, style)}"))
    return discord.ui.Container(*children, accent_colour=style.color)


def build_headline(event: NormalizedEvent) -> str:
    template = _REEL_HEADLINE if _is_reel(event) else _HEADLINES.get(event.event_type, "🆕 **{name}** has an update!")
    return template.format(name=_safe(discord.utils.escape_markdown(display_name(event))))


def build_plain_text(event: NormalizedEvent) -> str:
    """The text-only alert (embeds turned off): headline, title, link."""
    headline = build_headline(event)
    title = _safe(event.title or event.description or "").strip()
    text = f"{headline} {title}".strip() if event.event_type != EventType.STREAM_ENDED else headline
    return f"{text}\n{event.url}" if event.url and event.event_type != EventType.STREAM_ENDED else text


def display_name(event: NormalizedEvent) -> str:
    """The platform's display name (a YouTube channel's title) when known,
    else the handle the account was added with."""
    return event.author or event.account_username


def _style(event: NormalizedEvent) -> _PlatformStyle:
    return _STYLES.get(event.platform) or _PlatformStyle(
        name=event.platform.title(), color=_DEFAULT_COLOR, button_label="Open", footers={}
    )


def _footer(event: NormalizedEvent, style: _PlatformStyle) -> str:
    label = f"{style.name} Reels" if _is_reel(event) else style.footers.get(event.event_type, style.name)
    moment = event.started_at or event.published_at
    if moment is None:
        return label
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    # Discord renders <t:...> in each viewer's own timezone.
    return f"{label} · <t:{int(moment.timestamp())}:f>"


def _stream_details(event: NormalizedEvent) -> Optional[str]:
    """"🎮 Just Chatting · 👀 75,301 watching" for a stream that just went
    live, from whatever the platform reported."""
    if event.event_type != EventType.STREAM_STARTED:
        return None
    parts = []
    category = event.metadata.get("category")
    if category:
        parts.append(f"🎮 {_safe(str(category))}")
    viewers = event.metadata.get("viewer_count")
    if isinstance(viewers, int) and viewers > 0:
        parts.append(f"👀 {viewers:,} watching")
    return " · ".join(parts) or None


def _is_reel(event: NormalizedEvent) -> bool:
    """Instagram posts that are Reels (see platforms/instagram/parser.py)."""
    return event.event_type == EventType.POST_CREATED and bool(event.metadata.get("is_reel"))


def _account_url(event: NormalizedEvent) -> Optional[str]:
    if event.platform == "youtube":
        return f"https://www.youtube.com/channel/{event.platform_account_id}"
    if event.platform == "kick":
        return f"https://kick.com/{event.account_username.lstrip('@')}"
    if event.platform == "instagram":
        return f"https://www.instagram.com/{event.account_username.lstrip('@')}/"
    return None


def _safe(text: str) -> str:
    """Platform-sourced text must never be able to ping — see the module
    docstring."""
    return discord.utils.escape_mentions(text)


def _link_text(text: str) -> str:
    # A literal ] would end the [text](url) link early.
    return text.replace("[", "\\[").replace("]", "\\]")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
