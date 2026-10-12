"""Alert layouts (notifications/cards.py) — pure functions, no Discord API
or DB needed."""

from __future__ import annotations

import datetime
from typing import Any, List

import discord

from events.models import EventType, NormalizedEvent
from notifications.cards import build_alert, build_card, build_headline, build_plain_text
from ui.cards import plain_text


def _event(event_type: EventType = EventType.VIDEO_PUBLISHED, **overrides: Any) -> NormalizedEvent:
    fields = dict(
        platform="youtube",
        platform_account_id="UCabc",
        account_username="@example",
        event_type=event_type,
        platform_event_id="vid1",
        title="A great video",
        url="https://youtube.com/watch?v=vid1",
    )
    fields.update(overrides)
    return NormalizedEvent(**fields)


def _items(container: discord.ui.Container) -> List[Any]:
    view = discord.ui.LayoutView()
    view.add_item(container)
    return list(view.walk_children())


def _text(container: discord.ui.Container) -> str:
    view = discord.ui.LayoutView()
    view.add_item(container)
    return plain_text(view)


def test_youtube_live_card_matches_the_reference_layout() -> None:
    started = datetime.datetime(2026, 10, 12, 17, 42, tzinfo=datetime.timezone.utc)
    card = build_card(
        _event(
            EventType.STREAM_STARTED,
            title="vector games live",
            description="New to streaming? Check out StreamYard",
            author="Vector Games",
            author_avatar_url="https://yt3.example/avatar.jpg",
            thumbnail_url="https://i.ytimg.com/vi/vid1/maxresdefault_live.jpg",
            started_at=started,
        )
    )

    text = _text(card)
    assert card.accent_colour == 0xFF0000
    assert "**[Vector Games](https://www.youtube.com/channel/UCabc)**" in text
    assert "### [vector games live](https://youtube.com/watch?v=vid1)" in text
    assert "Vector Games is now live on YouTube!" in text
    assert "**Description**\nNew to streaming? Check out StreamYard" in text
    assert f"-# YouTube Live · <t:{int(started.timestamp())}:f>" in text
    items = _items(card)
    thumbnails = [item for item in items if isinstance(item, discord.ui.Thumbnail)]
    assert [t.media.url for t in thumbnails] == ["https://yt3.example/avatar.jpg"]
    galleries = [item for item in items if isinstance(item, discord.ui.MediaGallery)]
    assert [g.items[0].media.url for g in galleries] == ["https://i.ytimg.com/vi/vid1/maxresdefault_live.jpg"]
    buttons = [item for item in items if isinstance(item, discord.ui.Button)]
    assert [(b.label, b.url) for b in buttons] == [("Watch on YouTube", "https://youtube.com/watch?v=vid1")]


def test_footer_follows_the_event_type() -> None:
    assert "-# YouTube Shorts" in _text(build_card(_event(EventType.SHORT_PUBLISHED)))
    assert "-# YouTube" in _text(build_card(_event(EventType.VIDEO_PUBLISHED)))


def test_missing_pieces_are_left_out_rather_than_shown_empty() -> None:
    card = build_card(_event(description="  ", thumbnail_url=None, author_avatar_url=None))

    items = _items(card)
    assert not any(isinstance(item, (discord.ui.Thumbnail, discord.ui.MediaGallery)) for item in items)
    assert "Description" not in _text(card)


def test_long_descriptions_are_trimmed() -> None:
    text = _text(build_card(_event(description="x" * 2000)))
    description = text.split("**Description**\n", 1)[1].split("\n", 1)[0]
    assert len(description) <= 350


def test_kick_and_instagram_get_their_own_colour_and_link() -> None:
    kick = build_card(_event(EventType.STREAM_STARTED, platform="kick", account_username="streamer"))
    instagram = build_card(_event(EventType.POST_CREATED, platform="instagram", account_username="creator"))

    assert kick.accent_colour == 0x53FC18
    assert "[streamer](https://kick.com/streamer)" in _text(kick)
    assert instagram.accent_colour == 0xE1306C
    assert "creator shared a new post on Instagram!" in _text(instagram)


def test_stream_ended_card_has_no_button_or_media() -> None:
    card = build_card(_event(EventType.STREAM_ENDED, thumbnail_url="https://x/t.jpg", description="bye"))

    assert not any(isinstance(item, (discord.ui.Button, discord.ui.MediaGallery)) for item in _items(card))


def test_a_title_with_brackets_does_not_break_its_link() -> None:
    text = _text(build_card(_event(title="Part [2] finale")))
    assert "### [Part \\[2\\] finale](https://youtube.com/watch?v=vid1)" in text


def test_platform_text_in_the_card_cannot_ping() -> None:
    """Text inside a V2 message pings (unlike legacy embeds)."""
    text = _text(build_card(_event(title="@everyone look", description="@here", author="@everyone TV")))
    assert "@everyone" not in text
    assert "@here" not in text


def test_headline_uses_the_display_name_and_cannot_ping() -> None:
    assert build_headline(_event(EventType.STREAM_STARTED, author="Vector Games")) == "🔴 **Vector Games** is live now!"
    assert "@everyone" not in build_headline(_event(author="@everyone TV"))


def test_plain_text_has_the_title_and_link_and_cannot_ping() -> None:
    text = build_plain_text(_event(title="@everyone look"))
    assert text.endswith("\nhttps://youtube.com/watch?v=vid1")
    assert "@everyone" not in text


def test_plain_text_stream_ended_has_no_link() -> None:
    text = build_plain_text(_event(EventType.STREAM_ENDED))
    assert "\n" not in text and "ended" in text


def test_build_alert_puts_the_text_line_above_the_card() -> None:
    view = build_alert(_event(), top_line="<@&5> 🆕 hi", rich=True)

    children = list(view.children)
    assert isinstance(children[0], discord.ui.TextDisplay) and children[0].content == "<@&5> 🆕 hi"
    assert isinstance(children[1], discord.ui.Container)


def test_build_alert_text_only_when_embeds_are_off() -> None:
    view = build_alert(_event(), top_line="hello", rich=False)

    assert [type(child) for child in view.children] == [discord.ui.TextDisplay]


def test_instagram_reel_wording_and_caption_label() -> None:
    reel = _event(
        EventType.POST_CREATED,
        platform="instagram",
        account_username="nike",
        author="Nike",
        title=None,
        description="Five MVPs.",
        metadata={"is_reel": True},
    )

    text = _text(build_card(reel))
    assert build_headline(reel) == "🎬 **Nike** posted a new Reel!"
    assert "Nike posted a new Reel on Instagram!" in text
    assert "**Caption**\nFive MVPs." in text
    assert "-# Instagram Reels" in text


def test_instagram_feed_post_wording() -> None:
    post = _event(EventType.POST_CREATED, platform="instagram", account_username="nike", author="Nike", title=None)

    assert build_headline(post) == "🆕 **Nike** shared a new post!"
    assert "-# Instagram" in _text(build_card(post))
