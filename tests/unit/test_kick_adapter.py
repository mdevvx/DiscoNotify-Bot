"""KickAdapter's own logic — resolving a name to an AccountRef, and live
status for every monitored broadcaster from ONE batched /livestreams call
per tick. Driven through a fake KickClient (not real HTTP; that's covered
separately in test_kick_client.py)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pytest

from platforms.base import AccountRef
from platforms.kick.adapter import KickAdapter
from utils.errors import PermanentPlatformError

_ACCOUNT = AccountRef(platform="kick", platform_account_id="12345", username="somestreamer")
_OTHER = AccountRef(platform="kick", platform_account_id="777", username="other")


def _livestream(broadcaster_id: int, slug: str) -> Dict[str, Any]:
    return {
        "broadcaster_user_id": broadcaster_id,
        "slug": slug,
        "stream_title": "Live now",
        "started_at": "2026-10-12T13:31:08Z",
        "viewer_count": 42,
        "profile_picture": f"https://files.kick.com/{slug}.webp",
    }


class FakeKickClient:
    def __init__(self) -> None:
        self.channel_by_slug: Optional[Dict[str, Any]] = {"broadcaster_user_id": 12345, "slug": "somestreamer"}
        self.live: List[Dict[str, Any]] = []
        self.livestream_calls: List[List[str]] = []

    async def get_channel_by_slug(self, slug: str):
        return self.channel_by_slug

    async def get_livestreams(self, broadcaster_user_ids: Sequence[str]):
        ids = list(broadcaster_user_ids)
        self.livestream_calls.append(ids)
        return [item for item in self.live if str(item["broadcaster_user_id"]) in ids]

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _make_adapter(client: FakeKickClient, clock: Optional[Clock] = None) -> KickAdapter:
    return KickAdapter(client, clock=clock or Clock())  # type: ignore[arg-type]


async def test_validate_account_resolves_a_slug_to_an_account_ref() -> None:
    ref = await _make_adapter(FakeKickClient()).validate_account("https://kick.com/SomeStreamer")

    assert (ref.platform, ref.platform_account_id, ref.username) == ("kick", "12345", "somestreamer")


async def test_validate_account_names_the_channel_it_could_not_find() -> None:
    client = FakeKickClient()
    client.channel_by_slug = None

    with pytest.raises(PermanentPlatformError, match="no Kick channel named 'typoname'"):
        await _make_adapter(client).validate_account("typoname")


async def test_offline_when_the_broadcaster_is_not_in_the_live_list() -> None:
    status = await _make_adapter(FakeKickClient()).get_live_status(_ACCOUNT)

    assert status is not None and status.is_live is False


async def test_live_status_carries_the_session_id_and_avatar() -> None:
    client = FakeKickClient()
    client.live = [_livestream(12345, "somestreamer")]

    status = await _make_adapter(client).get_live_status(_ACCOUNT)

    assert status is not None and status.is_live is True
    assert status.stream_id == "12345:2026-10-12T13:31:08Z"
    assert status.author_avatar_url == "https://files.kick.com/somestreamer.webp"


async def test_every_account_is_served_by_one_batched_call_per_tick() -> None:
    client = FakeKickClient()
    clock = Clock()
    adapter = _make_adapter(client, clock)
    client.live = [_livestream(777, "other")]

    # First tick: the second account wasn't known yet when the first call
    # went out, so it triggers one more (batched) call.
    await adapter.get_live_status(_ACCOUNT)
    other = await adapter.get_live_status(_OTHER)
    calls_first_tick = len(client.livestream_calls)

    clock.now += 30
    await adapter.get_live_status(_ACCOUNT)
    await adapter.get_live_status(_OTHER)

    assert other is not None and other.is_live is True
    assert calls_first_tick == 2
    assert client.livestream_calls[-1] == ["12345", "777"]
    assert len(client.livestream_calls) == calls_first_tick + 1  # one call for both on the next tick


async def test_two_guilds_on_the_same_channel_share_the_call() -> None:
    client = FakeKickClient()
    adapter = _make_adapter(client)
    same_channel = AccountRef(platform="kick", platform_account_id="12345", username="somestreamer")

    await adapter.get_live_status(_ACCOUNT)
    await adapter.get_live_status(same_channel)

    assert len(client.livestream_calls) == 1


def test_poll_interval_does_not_grow_with_the_account_count() -> None:
    adapter = _make_adapter(FakeKickClient())

    assert adapter.poll_interval_seconds(1) == adapter.poll_interval_seconds(40) == 30.0


async def test_fetch_updates_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        await _make_adapter(FakeKickClient()).fetch_updates(_ACCOUNT, None)


async def test_normalize_event_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        _make_adapter(FakeKickClient()).normalize_event(_ACCOUNT, {})
