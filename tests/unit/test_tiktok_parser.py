"""TikTok parser: usernames and links, ordering, upload time, the alert."""

from __future__ import annotations

import datetime

import pytest

from events.models import EventType
from platforms.base import AccountRef
from platforms.tiktok import parser
from tests.unit.tiktok_fixtures import user, video, video_id
from utils.errors import PermanentPlatformError

_ACCOUNT = AccountRef(platform="tiktok", platform_account_id="107955", username="nasa")


@pytest.mark.parametrize(
    "typed",
    [
        "nasa",
        "@nasa",
        "  @nasa ",
        "https://www.tiktok.com/@nasa",
        "tiktok.com/@nasa/",
        "https://www.tiktok.com/@nasa?lang=en",
        "https://m.tiktok.com/@nasa/video/7694797676306615583",
    ],
)
def test_every_spelling_gives_the_username(typed: str) -> None:
    assert parser.parse_identifier(typed) == "nasa"


def test_dots_and_underscores_are_allowed() -> None:
    assert parser.parse_identifier("@khaby.lame") == "khaby.lame"
    assert parser.parse_identifier("a_b") == "a_b"


@pytest.mark.parametrize("typed", ["", "@", "a", "x" * 25, "has space", "bad!name", "ends.", "https://example.com/@nasa"])
def test_things_that_cant_be_usernames_are_refused(typed: str) -> None:
    with pytest.raises(PermanentPlatformError, match="TikTok username"):
        parser.parse_identifier(typed)


@pytest.mark.parametrize("typed", ["https://vm.tiktok.com/ZMabc123/", "https://www.tiktok.com/t/ZTabc123/"])
def test_short_links_say_to_use_the_profile_link(typed: str) -> None:
    with pytest.raises(PermanentPlatformError, match="short link"):
        parser.parse_identifier(typed)


def test_the_numeric_id_is_the_identity() -> None:
    assert parser.user_to_account_ref(user()) == _ACCOUNT


def test_videos_are_oldest_first_and_private_ones_dropped() -> None:
    videos = [video(3), video(1, private=True), video(5), {"desc": "no id"}]

    assert [v["id"] for v in parser.public_videos(videos)] == [video_id(3), video_id(5)]
    assert parser.newest_id(videos) == int(video_id(5))
    assert parser.newest_id([]) is None


def test_upload_time_comes_from_the_video_id() -> None:
    published = parser.video_published_at(video_id(0))

    assert published == datetime.datetime.fromtimestamp(1_790_000_000, tz=datetime.timezone.utc)
    assert parser.video_published_at("not-a-number") is None


def test_the_alert_has_everything_the_card_shows() -> None:
    event = parser.normalize_video(_ACCOUNT, {"video": video(2), "user": user()})

    assert event.event_type == EventType.VIDEO_PUBLISHED
    assert event.platform_event_id == video_id(2)
    assert event.url == f"https://www.tiktok.com/@nasa/video/{video_id(2)}"
    assert event.description == "caption 2"
    assert event.thumbnail_url == "https://cdn.example/origin2.jpg"
    assert event.author == "NASA" and event.author_avatar_url == "https://cdn.example/avatar.jpg"
    assert event.published_at is not None


def test_a_video_without_a_caption_or_origin_cover_still_normalizes() -> None:
    bare = {"id": video_id(1), "desc": "  ", "coverUrl": "https://cdn.example/c.jpg"}

    event = parser.normalize_video(_ACCOUNT, {"video": bare, "user": {}})

    assert event.description is None
    assert event.thumbnail_url == "https://cdn.example/c.jpg"
    assert event.author == "nasa" and event.url.startswith("https://www.tiktok.com/@nasa/video/")
