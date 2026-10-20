"""YouTubeAdapter's own logic: cursor/baseline behavior, live and Premiere
detection from the uploads playlist, per-channel snapshot sharing, the
daily quota gate, and the quota-driven poll interval — driven through a
fake YouTubeClient (not real HTTP; that's covered separately in
test_youtube_client.py)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from platforms.base import AccountRef
from platforms.youtube.adapter import YouTubeAdapter

_ACCOUNT = AccountRef(platform="youtube", platform_account_id="UCabc", username="example")

_CHANNEL = {
    "id": "UCabc",
    "snippet": {"title": "Example", "customUrl": "@example"},
    "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}},
}


def _playlist_item(video_id: str, added_at: str = "2026-09-15T00:00:00Z") -> Dict[str, Any]:
    return {"snippet": {"resourceId": {"videoId": video_id}, "publishedAt": added_at}}


def _video(video_id: str, duration: str = "PT10M0S") -> Dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {"title": video_id, "thumbnails": {}, "liveBroadcastContent": "none"},
        "contentDetails": {"duration": duration},
    }


def _stream(video_id: str, state: str, *, duration: str = "P0D", started: Optional[str] = None) -> Dict[str, Any]:
    """A real stream reports P0D; a Premiere passes its real duration."""
    details: Dict[str, Any] = {"scheduledStartTime": "2026-09-15T10:00:00Z"}
    if started:
        details["actualStartTime"] = started
    return {
        "id": video_id,
        "snippet": {"title": video_id, "liveBroadcastContent": state},
        "contentDetails": {"duration": duration},
        "liveStreamingDetails": details,
    }


class FakeYouTubeClient:
    def __init__(self) -> None:
        self.channel: Optional[Dict[str, Any]] = _CHANNEL
        self.uploads: List[Dict[str, Any]] = []
        self.videos_by_id: Dict[str, Dict[str, Any]] = {}
        self.get_channel_calls = 0
        self.list_uploads_calls = 0
        self.get_videos_requests: List[List[str]] = []

    async def get_channel(self, *, channel_id=None, handle=None, username=None):
        self.get_channel_calls += 1
        return self.channel

    async def list_uploads(self, uploads_playlist_id: str, *, max_results: int = 10):
        self.list_uploads_calls += 1
        return self.uploads[:max_results]

    async def get_videos(self, video_ids: List[str]):
        self.get_videos_requests.append(list(video_ids))
        return [self.videos_by_id[vid] for vid in video_ids if vid in self.videos_by_id]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def next_tick(self, adapter: YouTubeAdapter) -> None:
        self.now += adapter._poll_interval


def _make_adapter(client: FakeYouTubeClient, clock: Optional[Clock] = None, **kwargs) -> YouTubeAdapter:
    defaults: Dict[str, Any] = {"daily_quota_units": 10_000}
    defaults.update(kwargs)
    return YouTubeAdapter(client, clock=clock or Clock(), **defaults)  # type: ignore[arg-type]


# ── new uploads ─────────────────────────────────────────────────────────


async def test_first_poll_establishes_a_baseline_without_emitting_events() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-3", "2026-09-15T03:00:00Z"), _playlist_item("vid-2")]
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor=None)

    assert result.items == []
    assert result.next_cursor == "vid-3|2026-09-15T03:00:00Z"


async def test_subsequent_poll_returns_only_items_newer_than_the_cursor_oldest_first() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-3"), _playlist_item("vid-2"), _playlist_item("vid-1")]
    client.videos_by_id = {vid: _video(vid) for vid in ("vid-1", "vid-2", "vid-3")}
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")  # pre-2026-10-12 bare-ID cursor

    assert [v["id"] for v in result.items] == ["vid-2", "vid-3"]
    assert result.next_cursor.startswith("vid-3|")


async def test_no_new_items_returns_empty() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-1")]
    client.videos_by_id = {"vid-1": _video("vid-1")}
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")

    assert result.items == []


async def test_deleted_cursor_video_only_reports_uploads_added_after_it() -> None:
    """Regression test: the cursor's video being deleted or made private
    used to make every upload on the page look new, re-announcing up to 10
    old videos."""
    client = FakeYouTubeClient()
    client.uploads = [
        _playlist_item("vid-new", "2026-09-15T05:00:00Z"),
        _playlist_item("vid-old", "2026-09-15T01:00:00Z"),
    ]
    client.videos_by_id = {"vid-new": _video("vid-new"), "vid-old": _video("vid-old")}
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-deleted|2026-09-15T03:00:00Z")

    assert [v["id"] for v in result.items] == ["vid-new"]


async def test_missing_legacy_cursor_rebaselines_instead_of_flooding() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-2"), _playlist_item("vid-1")]
    client.videos_by_id = {"vid-2": _video("vid-2"), "vid-1": _video("vid-1")}
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-deleted")

    assert result.items == []
    assert result.next_cursor.startswith("vid-2|")


async def test_fetch_updates_excludes_livestreams_from_new_video_results() -> None:
    """A stream going live (or one that already ended) shows up in the
    uploads playlist exactly like a regular upload — must never be
    reported as a 'new video' (that's get_live_status()'s job). Regression
    test for a real production bug: a livestream was announced as 'New
    Video' instead of 'Live Started'."""
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("live-1"), _playlist_item("vid-1"), _playlist_item("vid-0")]
    client.videos_by_id = {
        "live-1": _stream("live-1", "live", started="2026-09-15T10:00:00Z"),
        "vid-1": _video("vid-1"),
        "vid-0": _video("vid-0"),
    }
    adapter = _make_adapter(client)

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-0")

    assert [v["id"] for v in result.items] == ["vid-1"]
    assert result.next_cursor.startswith("live-1|")  # cursor still advances past the excluded item


async def test_regular_video_details_are_not_refetched_every_check() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("vid-1")]
    client.videos_by_id = {"vid-1": _video("vid-1")}
    adapter = _make_adapter(client, clock)

    await adapter.fetch_updates(_ACCOUNT, cursor=None)
    clock.next_tick(adapter)
    await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")

    assert client.list_uploads_calls == 2
    assert client.get_videos_requests == [["vid-1"]]


async def test_missing_channel_raises_permanent_error() -> None:
    from utils.errors import PermanentPlatformError

    client = FakeYouTubeClient()
    client.channel = None
    adapter = _make_adapter(client)

    with pytest.raises(PermanentPlatformError):
        await adapter.fetch_updates(_ACCOUNT, cursor=None)


async def test_uploads_playlist_id_is_cached_after_the_first_lookup() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("vid-1")]
    adapter = _make_adapter(client, clock)

    for _ in range(3):
        await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")
        clock.next_tick(adapter)

    assert client.get_channel_calls == 1  # not once per poll


# ── live streams ────────────────────────────────────────────────────────


async def test_offline_when_nothing_is_live() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-1")]
    client.videos_by_id = {"vid-1": _video("vid-1")}
    adapter = _make_adapter(client)

    status = await adapter.get_live_status(_ACCOUNT)

    assert status is not None and status.is_live is False


async def test_a_stream_at_the_top_of_the_uploads_playlist_is_reported_live() -> None:
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("live-1"), _playlist_item("vid-1")]
    client.videos_by_id = {"live-1": _stream("live-1", "live", started="2026-09-15T10:00:00Z"), "vid-1": _video("vid-1")}
    client.videos_by_id["live-1"]["liveStreamingDetails"]["concurrentViewers"] = "42"
    adapter = _make_adapter(client)

    status = await adapter.get_live_status(_ACCOUNT)

    assert status is not None and status.is_live is True
    assert status.stream_id == "live-1"
    assert status.viewer_count == 42


async def test_upcoming_stream_is_tracked_until_it_goes_live_and_then_ends() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("stream-1")]
    client.videos_by_id = {"stream-1": _stream("stream-1", "upcoming")}
    adapter = _make_adapter(client, clock)

    upcoming = await adapter.get_live_status(_ACCOUNT)
    client.videos_by_id["stream-1"] = _stream("stream-1", "live", started="2026-09-15T10:00:00Z")
    clock.next_tick(adapter)
    live = await adapter.get_live_status(_ACCOUNT)
    client.videos_by_id["stream-1"] = _stream("stream-1", "none", started="2026-09-15T10:00:00Z")
    clock.next_tick(adapter)
    ended = await adapter.get_live_status(_ACCOUNT)

    assert upcoming is not None and upcoming.is_live is False
    assert live is not None and live.is_live is True
    assert ended is not None and ended.is_live is False
    # The tracked broadcast's state is refreshed every check, not cached.
    assert all("stream-1" in ids for ids in client.get_videos_requests)


async def test_a_tracked_stream_that_scrolled_off_the_page_is_still_checked() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("stream-1")]
    client.videos_by_id = {"stream-1": _stream("stream-1", "upcoming")}
    adapter = _make_adapter(client, clock)
    await adapter.get_live_status(_ACCOUNT)

    client.uploads = [_playlist_item(f"vid-{i}") for i in range(10)]
    client.videos_by_id.update({f"vid-{i}": _video(f"vid-{i}") for i in range(10)})
    client.videos_by_id["stream-1"] = _stream("stream-1", "live", started="2026-09-15T10:00:00Z")
    clock.next_tick(adapter)
    status = await adapter.get_live_status(_ACCOUNT)

    assert status is not None and status.is_live is True
    assert status.stream_id == "stream-1"


# ── Premieres ───────────────────────────────────────────────────────────


async def test_a_premiere_is_announced_as_a_new_video_when_it_starts_not_as_a_stream() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("prem-1"), _playlist_item("vid-0")]
    client.videos_by_id = {"prem-1": _stream("prem-1", "upcoming", duration="PT12M"), "vid-0": _video("vid-0")}
    adapter = _make_adapter(client, clock)

    scheduled = await adapter.fetch_updates(_ACCOUNT, cursor="vid-0")
    client.videos_by_id["prem-1"] = _stream("prem-1", "live", duration="PT12M", started="2026-09-15T10:00:00Z")
    clock.next_tick(adapter)
    started = await adapter.fetch_updates(_ACCOUNT, cursor=scheduled.next_cursor)
    live_status = await adapter.get_live_status(_ACCOUNT)

    assert scheduled.items == []
    assert [v["id"] for v in started.items] == ["prem-1"]
    assert live_status is not None and live_status.is_live is False


async def test_a_premiere_that_starts_and_finishes_between_checks_is_still_announced() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("prem-1"), _playlist_item("vid-0")]
    client.videos_by_id = {"prem-1": _stream("prem-1", "upcoming", duration="PT20S"), "vid-0": _video("vid-0")}
    adapter = _make_adapter(client, clock)

    scheduled = await adapter.fetch_updates(_ACCOUNT, cursor="vid-0")
    client.videos_by_id["prem-1"] = _stream("prem-1", "none", duration="PT20S", started="2026-09-15T10:00:00Z")
    clock.next_tick(adapter)
    result = await adapter.fetch_updates(_ACCOUNT, cursor=scheduled.next_cursor)

    assert [v["id"] for v in result.items] == ["prem-1"]


# ── sharing, quota, cadence ─────────────────────────────────────────────


async def test_two_guilds_on_the_same_channel_share_one_check_per_tick() -> None:
    """Two guilds monitoring the same creator each get their own AccountRef
    with the same platform_account_id — without sharing, each would pay for
    its own API calls against the one shared daily quota."""
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-1")]
    client.videos_by_id = {"vid-1": _video("vid-1")}
    adapter = _make_adapter(client)
    same_channel_other_guild = AccountRef(platform="youtube", platform_account_id="UCabc", username="example")

    await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")
    await adapter.get_live_status(_ACCOUNT)
    await adapter.fetch_updates(same_channel_other_guild, cursor="vid-1")
    await adapter.get_live_status(same_channel_other_guild)

    assert client.list_uploads_calls == 1


async def test_the_next_tick_checks_again() -> None:
    client = FakeYouTubeClient()
    clock = Clock()
    client.uploads = [_playlist_item("vid-1")]
    adapter = _make_adapter(client, clock)

    await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")
    clock.next_tick(adapter)
    await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")

    assert client.list_uploads_calls == 2


async def test_exhausted_quota_skips_the_check_without_reporting_offline() -> None:
    """None, not is_live=False — "offline" would make the worker announce
    that a running stream ended."""
    client = FakeYouTubeClient()
    client.uploads = [_playlist_item("vid-1")]
    adapter = _make_adapter(client, daily_quota_units=1)  # only the channels.list lookup fits

    result = await adapter.fetch_updates(_ACCOUNT, cursor="vid-1")
    status = await adapter.get_live_status(_ACCOUNT)

    assert result.items == [] and result.next_cursor == "vid-1"
    assert status is None
    assert client.list_uploads_calls == 0


def test_poll_interval_is_the_floor_for_a_single_channel() -> None:
    adapter = _make_adapter(FakeYouTubeClient())

    assert adapter.poll_interval_seconds(1) == 30.0


def test_poll_interval_stretches_with_the_channel_count_to_fit_the_daily_quota() -> None:
    """Each channel costs up to 2 units per check against 9,000 usable
    units/day (10% headroom), so 10 channels need 86400*10*2/9000 = 192s."""
    adapter = _make_adapter(FakeYouTubeClient())

    assert adapter.poll_interval_seconds(5) == pytest.approx(96.0)
    assert adapter.poll_interval_seconds(10) == pytest.approx(192.0)


def test_poll_interval_respects_a_configured_floor() -> None:
    adapter = _make_adapter(FakeYouTubeClient(), min_poll_interval_seconds=120.0)

    assert adapter.poll_interval_seconds(1) == 120.0


async def test_a_channel_added_before_its_first_upload_still_announces_that_upload() -> None:
    """Regression test: a channel with no uploads got no baseline, so its
    first upload became the baseline on the next check and was never
    announced (same bug as found on Instagram 2026-10-12)."""
    client = FakeYouTubeClient()
    clock = Clock()
    adapter = _make_adapter(client, clock)

    baseline = await adapter.fetch_updates(_ACCOUNT, cursor=None)
    assert baseline.items == [] and baseline.next_cursor is not None

    client.uploads = [_playlist_item("first", "2099-01-01T00:00:00Z")]
    client.videos_by_id = {"first": _video("first")}
    clock.next_tick(adapter)
    result = await adapter.fetch_updates(_ACCOUNT, cursor=baseline.next_cursor)

    assert [video["id"] for video in result.items] == ["first"]


async def test_quota_snapshot_reports_the_units_left_of_the_daily_budget() -> None:
    adapter = _make_adapter(FakeYouTubeClient(), daily_quota_units=100)
    assert adapter.quota_snapshot() == (100, 100)

    await adapter._budget.try_acquire(cost=30)

    assert adapter.quota_snapshot() == (70, 100)
