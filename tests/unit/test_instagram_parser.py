"""Pure parsing logic — no HTTP, no adapter state. Fixture JSON shapes
mirror the real Instagram Graph API business_discovery response shapes."""

from __future__ import annotations

from events.models import EventType
from platforms.base import AccountRef
from platforms.instagram import parser


# ── parse_identifier ────────────────────────────────────────────────────


def test_parse_identifier_lowercases_a_bare_username() -> None:
    assert parser.parse_identifier("SomeCreator") == "somecreator"


def test_parse_identifier_strips_a_leading_at_sign() -> None:
    assert parser.parse_identifier("@SomeCreator") == "somecreator"


def test_parse_identifier_parses_a_profile_url() -> None:
    assert parser.parse_identifier("https://www.instagram.com/SomeCreator") == "somecreator"


def test_parse_identifier_strips_trailing_path_and_query_from_url() -> None:
    assert parser.parse_identifier("https://instagram.com/somecreator/reels/?hl=en") == "somecreator"


def test_parse_identifier_strips_surrounding_whitespace() -> None:
    assert parser.parse_identifier("  somecreator  ") == "somecreator"


# ── discovery_to_account_ref ──────────────────────────────────────────────


def test_discovery_to_account_ref_uses_username_not_display_name() -> None:
    discovery = {"id": "17841400", "username": "somecreator", "name": "Some Creator Inc."}
    ref = parser.discovery_to_account_ref(discovery)
    assert ref.platform == "instagram"
    assert ref.platform_account_id == "17841400"
    assert ref.username == "somecreator"


def test_discovery_to_account_ref_falls_back_to_id_without_username() -> None:
    discovery = {"id": "17841400"}
    ref = parser.discovery_to_account_ref(discovery)
    assert ref.username == "17841400"


# ── normalize_media ─────────────────────────────────────────────────────

_ACCOUNT = AccountRef(platform="instagram", platform_account_id="17841400", username="somecreator")


def _media(**overrides) -> dict:
    media = {
        "id": "18000000000000000",
        "caption": "A great post",
        "timestamp": "2026-09-15T12:00:00+0000",
        "media_type": "IMAGE",
        "media_url": "https://example.com/media.jpg",
        "permalink": "https://www.instagram.com/p/abc123/",
    }
    media.update(overrides)
    return media


def test_normalize_media_produces_post_created() -> None:
    event = parser.normalize_media(_ACCOUNT, _media())
    assert event.event_type == EventType.POST_CREATED
    assert event.platform_event_id == "18000000000000000"
    assert event.url == "https://www.instagram.com/p/abc123/"


def test_normalize_media_carries_the_caption_as_description() -> None:
    event = parser.normalize_media(_ACCOUNT, _media(caption="hello"))
    assert event.description == "hello"


def test_normalize_media_records_media_type_in_metadata() -> None:
    event = parser.normalize_media(_ACCOUNT, _media(media_type="VIDEO"))
    assert event.metadata["media_type"] == "VIDEO"


def test_normalize_media_parses_the_timestamp() -> None:
    event = parser.normalize_media(_ACCOUNT, _media())
    assert event.published_at is not None


def test_normalize_media_handles_missing_timestamp() -> None:
    media = _media()
    del media["timestamp"]
    event = parser.normalize_media(_ACCOUNT, media)
    assert event.published_at is None


def test_normalize_media_flags_a_reel() -> None:
    reel = parser.normalize_media(_ACCOUNT, _media(media_type="VIDEO", media_product_type="REELS"))
    post = parser.normalize_media(_ACCOUNT, _media(media_product_type="FEED"))
    assert reel.metadata["is_reel"] is True
    assert post.metadata["is_reel"] is False


def test_normalize_media_uses_a_videos_thumbnail_not_the_video_file() -> None:
    event = parser.normalize_media(
        _ACCOUNT,
        _media(media_type="VIDEO", media_url="https://cdn.example/clip.mp4", thumbnail_url="https://cdn.example/still.jpg"),
    )
    assert event.thumbnail_url == "https://cdn.example/still.jpg"


def test_normalize_media_uses_the_profile_name_and_picture() -> None:
    media = _media(_profile={"name": "Some Creator", "profile_picture_url": "https://cdn.example/a.jpg"})
    event = parser.normalize_media(_ACCOUNT, media)
    assert event.author == "Some Creator"
    assert event.author_avatar_url == "https://cdn.example/a.jpg"


def test_normalize_media_falls_back_to_the_username_without_a_profile_name() -> None:
    assert parser.normalize_media(_ACCOUNT, _media()).author == "somecreator"


def test_normalize_media_parses_metas_offset_format_exactly() -> None:
    event = parser.normalize_media(_ACCOUNT, _media(timestamp="2026-10-11T20:36:14+0000"))
    assert event.published_at is not None and event.published_at.utcoffset().total_seconds() == 0
    assert (event.published_at.hour, event.published_at.minute) == (20, 36)
