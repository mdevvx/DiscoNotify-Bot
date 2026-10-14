"""X parser: handles from user input, which posts count as new posts, and
the alert event built from an embedded-timeline post."""

from __future__ import annotations

import datetime

import pytest

from events.models import EventType
from platforms.base import AccountRef
from platforms.twitter import parser
from tests.unit.twitter_fixtures import NASA, photo_post, post, retweet
from utils.errors import PermanentPlatformError

_ACCOUNT = AccountRef(platform="twitter", platform_account_id="11348282", username="NASA")


@pytest.mark.parametrize(
    "value",
    [
        "NASA",
        "@NASA",
        "  @NASA ",
        "https://x.com/NASA",
        "https://twitter.com/NASA/status/123?s=20",
        "https://mobile.twitter.com/NASA",
        "https://www.x.com/NASA",
    ],
)
def test_parse_identifier_accepts_handles_and_profile_urls(value: str) -> None:
    assert parser.parse_identifier(value) == "NASA"


@pytest.mark.parametrize("value", ["", "not a handle", "https://x.com/home", "waytoolonghandle_123", "@@"])
def test_parse_identifier_rejects_what_cant_be_a_handle(value: str) -> None:
    with pytest.raises(PermanentPlatformError):
        parser.parse_identifier(value)


def test_account_ref_keeps_the_id_and_the_handle() -> None:
    assert parser.user_to_account_ref(NASA) == _ACCOUNT


def test_retweets_and_replies_are_left_out_and_posts_come_oldest_first() -> None:
    posts = [post("30"), retweet("25"), post("20", in_reply_to_status_id_str="1"), post("10", is_quote_status=True)]

    assert [p["id_str"] for p in parser.original_posts(posts)] == ["10", "30"]


def test_a_photo_post_becomes_a_full_alert() -> None:
    event = parser.normalize_tweet(_ACCOUNT, photo_post())

    assert event.event_type == EventType.POST_CREATED
    assert event.platform_event_id == "2110389272024867195"
    assert event.url == "https://x.com/NASA/status/2110389272024867195"
    assert event.description == "Watch it on our YouTube channel: https://youtu.be/5b3n-u_0QAI"
    assert event.thumbnail_url == "https://pbs.twimg.com/media/HUmb2vRWwAAFQ3D.jpg"
    assert event.author == "NASA"
    assert event.author_avatar_url == "https://pbs.twimg.com/profile_images/1321163587679784960/0ZxKlEKB_400x400.jpg"
    assert event.published_at == datetime.datetime(2026, 10, 14, 15, 16, 26, tzinfo=datetime.timezone.utc)
    assert event.metadata == {"is_quote": False, "media_type": "photo", "media_count": 1}


def test_a_text_post_has_no_image() -> None:
    event = parser.normalize_tweet(_ACCOUNT, post("5", is_quote_status=True))

    assert event.description == "post 5"
    assert event.thumbnail_url is None
    assert event.metadata["is_quote"] is True
