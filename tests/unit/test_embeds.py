"""Notification body builders (rich embed and plain text) — pure functions,
no Discord API or DB needed."""

from __future__ import annotations

from events.models import EventType, NormalizedEvent
import datetime

from notifications.embeds import build_embed, build_embed_headline, build_plain_text


def _event(event_type: EventType, **overrides) -> NormalizedEvent:
    fields = dict(
        platform="youtube",
        platform_account_id="UCabc",
        account_username="Example Creator",
        event_type=event_type,
        platform_event_id="vid1",
        title="A great video",
        url="https://youtube.com/watch?v=vid1",
    )
    fields.update(overrides)
    return NormalizedEvent(**fields)


def test_plain_text_includes_title_and_url() -> None:
    text = build_plain_text(_event(EventType.VIDEO_PUBLISHED))
    assert "A great video" in text
    assert "https://youtube.com/watch?v=vid1" in text


def test_plain_text_stream_started_has_no_trailing_url_when_missing() -> None:
    text = build_plain_text(_event(EventType.STREAM_STARTED, url=None))
    assert text.count("\n") == 0


def test_plain_text_stream_ended_mentions_the_creator() -> None:
    text = build_plain_text(_event(EventType.STREAM_ENDED))
    assert "Example Creator" in text
    assert "ended" in text.lower()


def test_plain_text_cannot_be_made_to_ping_by_a_video_title() -> None:
    """Regression test: titles went into the message content unescaped, and
    the sender allows an @everyone ping whenever that text appears — so a
    creator could ping a whole server by titling a video "@everyone"."""
    text = build_plain_text(_event(EventType.VIDEO_PUBLISHED, title="@everyone look"))
    assert "@everyone" not in text


# ── rich embed ───────────────────────────────────────────────────────────


def test_youtube_live_embed_matches_the_reference_layout() -> None:
    started = datetime.datetime(2026, 10, 12, 17, 42, tzinfo=datetime.timezone.utc)
    event = _event(
        EventType.STREAM_STARTED,
        title="vector games live",
        description="New to streaming? Check out StreamYard",
        author="Vector Games",
        author_avatar_url="https://yt3.example/avatar.jpg",
        thumbnail_url="https://i.ytimg.com/vi/vid1/maxresdefault_live.jpg",
        started_at=started,
    )

    embed = build_embed(event)

    assert embed is not None
    assert embed.author.name == "Vector Games"
    assert embed.author.icon_url == "https://yt3.example/avatar.jpg"
    assert embed.title == "vector games live"
    assert embed.url == "https://youtube.com/watch?v=vid1"
    assert embed.description == "Vector Games is now live on YouTube!"
    assert embed.fields[0].name == "Description"
    assert embed.fields[0].value == "New to streaming? Check out StreamYard"
    assert embed.thumbnail.url == "https://yt3.example/avatar.jpg"
    assert embed.image.url == "https://i.ytimg.com/vi/vid1/maxresdefault_live.jpg"
    assert embed.footer.text == "YouTube Live"
    assert embed.timestamp == started


def test_embed_footer_and_headline_follow_the_event_type() -> None:
    short = build_embed(_event(EventType.SHORT_PUBLISHED))
    video = build_embed(_event(EventType.VIDEO_PUBLISHED))
    assert short is not None and short.footer.text == "YouTube Shorts"
    assert video is not None and video.footer.text == "YouTube"


def test_long_descriptions_are_trimmed_and_empty_ones_omitted() -> None:
    long_embed = build_embed(_event(EventType.VIDEO_PUBLISHED, description="x" * 2000))
    empty_embed = build_embed(_event(EventType.VIDEO_PUBLISHED, description="  "))
    assert long_embed is not None and len(long_embed.fields[0].value) <= 350
    assert empty_embed is not None and empty_embed.fields == []


def test_embed_falls_back_to_the_handle_without_a_display_name() -> None:
    embed = build_embed(_event(EventType.VIDEO_PUBLISHED, author=None))
    assert embed is not None and embed.author.name == "Example Creator"


def test_no_embed_for_platforms_without_the_data_for_one() -> None:
    assert build_embed(_event(EventType.STREAM_STARTED, platform="kick")) is None


def test_no_embed_for_stream_ended() -> None:
    assert build_embed(_event(EventType.STREAM_ENDED)) is None


def test_embed_headline_wraps_the_link_to_avoid_a_duplicate_preview() -> None:
    headline = build_embed_headline(_event(EventType.STREAM_STARTED, author="Vector Games"))
    assert headline == "🔴 **Vector Games** is live now! <https://youtube.com/watch?v=vid1>"


def test_embed_headline_cannot_be_made_to_ping_by_a_channel_name() -> None:
    headline = build_embed_headline(_event(EventType.STREAM_STARTED, author="@everyone TV"))
    assert "@everyone" not in headline
