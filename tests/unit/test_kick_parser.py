"""Pure parsing logic — no HTTP, no adapter state. Fixture JSON shapes
mirror the real Kick Public API response shapes documented at
docs.kick.com/apis/channels."""

from __future__ import annotations

from platforms.kick import parser


# ── parse_identifier ────────────────────────────────────────────────────


def test_parse_identifier_lowercases_a_bare_slug() -> None:
    assert parser.parse_identifier("SomeStreamer") == "somestreamer"


def test_parse_identifier_strips_a_leading_at_sign() -> None:
    assert parser.parse_identifier("@SomeStreamer") == "somestreamer"


def test_parse_identifier_parses_a_channel_url() -> None:
    assert parser.parse_identifier("https://www.kick.com/SomeStreamer") == "somestreamer"


def test_parse_identifier_parses_a_bare_domain_url() -> None:
    assert parser.parse_identifier("https://kick.com/somestreamer") == "somestreamer"


def test_parse_identifier_strips_trailing_path_and_query_from_url() -> None:
    assert parser.parse_identifier("https://kick.com/somestreamer/videos?filter=all") == "somestreamer"


def test_parse_identifier_strips_surrounding_whitespace() -> None:
    assert parser.parse_identifier("  somestreamer  ") == "somestreamer"


# ── channel_to_account_ref ────────────────────────────────────────────────


def test_channel_to_account_ref_uses_the_stable_numeric_id() -> None:
    channel = {"broadcaster_user_id": 12345, "slug": "somestreamer"}
    ref = parser.channel_to_account_ref(channel)
    assert ref.platform == "kick"
    assert ref.platform_account_id == "12345"
    assert ref.username == "somestreamer"


def test_channel_to_account_ref_falls_back_to_id_without_a_slug() -> None:
    channel = {"broadcaster_user_id": 12345, "slug": ""}
    ref = parser.channel_to_account_ref(channel)
    assert ref.username == "12345"


# ── livestream_to_live_status ──────────────────────────────────────────


def _livestream(**overrides) -> dict:
    """Shape of a GET /public/v1/livestreams item, from the live API."""
    item = {
        "broadcaster_user_id": 12345,
        "channel_id": 999,
        "slug": "somestreamer",
        "stream_title": "Playing games",
        "viewer_count": 1234,
        "thumbnail": "https://images.kick.com/video_thumbnails/x/480.webp",
        "started_at": "2026-10-12T13:31:08Z",
        "category": {"id": 15, "name": "Just Chatting"},
        "profile_picture": "https://files.kick.com/images/user/12345/profile.webp",
    }
    item.update(overrides)
    return item


def test_livestream_is_live_with_title_category_and_viewers() -> None:
    status = parser.livestream_to_live_status(_livestream())
    assert status.is_live is True
    assert status.title == "Playing games"
    assert status.category == "Just Chatting"
    assert status.viewer_count == 1234
    assert status.started_at is not None and status.started_at.hour == 13


def test_livestream_session_id_is_stable_within_a_session_and_differs_between_sessions() -> None:
    """Kick has no stream ID; one built from broadcaster + start time keeps
    each session's alerts distinct (the "ended" alert used to work only once)."""
    first = parser.livestream_to_live_status(_livestream())
    same = parser.livestream_to_live_status(_livestream(viewer_count=5))
    later = parser.livestream_to_live_status(_livestream(started_at="2026-10-13T09:00:00Z"))
    assert first.stream_id == same.stream_id == "12345:2026-10-12T13:31:08Z"
    assert later.stream_id != first.stream_id


def test_livestream_carries_the_avatar_thumbnail_and_channel_url() -> None:
    status = parser.livestream_to_live_status(_livestream())
    assert status.author == "somestreamer"
    assert status.author_avatar_url == "https://files.kick.com/images/user/12345/profile.webp"
    assert status.thumbnail_url == "https://images.kick.com/video_thumbnails/x/480.webp"
    assert status.url == "https://kick.com/somestreamer"


def test_livestream_handles_missing_category_and_start_time() -> None:
    status = parser.livestream_to_live_status(_livestream(category=None, started_at=None))
    assert status.category is None
    assert status.stream_id is None
