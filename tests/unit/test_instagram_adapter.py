"""InstagramAdapter's own logic: cursor/baseline behaviour (timestamp-based,
so a deleted post can't re-announce old ones), the by-username re-query
Business Discovery requires, one lookup per account per tick shared by
every guild, the hourly call budget, and the quota-driven poll interval.
Driven through a fake InstagramClient (not real HTTP; that's covered
separately in test_instagram_client.py)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import pytest

from platforms.base import AccountRef
from platforms.instagram.adapter import InstagramAdapter
from utils.errors import PermanentPlatformError

_ACCOUNT = AccountRef(platform="instagram", platform_account_id="17841400", username="somecreator")


def _media(media_id: str, timestamp: str, product: str = "FEED") -> Dict[str, Any]:
    return {"id": media_id, "caption": f"post {media_id}", "timestamp": timestamp, "media_product_type": product}


class FakeInstagramClient:
    def __init__(self) -> None:
        self.discovery: Optional[Dict[str, Any]] = {
            "id": "17841400",
            "username": "somecreator",
            "name": "Some Creator",
            "profile_picture_url": "https://cdn.example/avatar.jpg",
            "media": [],
        }
        self.seen_usernames: List[str] = []
        self.app_usage_percent: Optional[int] = None
        self.token_info: Dict[str, Any] = {
            "is_valid": True,
            "type": "PAGE",
            "expires_at": 0,
            "data_access_expires_at": 0,
            "scopes": ["instagram_basic", "instagram_manage_insights", "pages_read_engagement", "pages_show_list"],
        }

    async def get_token_info(self) -> Dict[str, Any]:
        return self.token_info

    async def get_business_discovery(self, username: str):
        self.seen_usernames.append(username)
        return self.discovery

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _make_adapter(client: FakeInstagramClient, clock: Optional[Clock] = None, **kwargs: Any) -> InstagramAdapter:
    return InstagramAdapter(client, clock=clock or Clock(), **kwargs)  # type: ignore[arg-type]


def _next_tick(adapter: InstagramAdapter, clock: Clock) -> None:
    clock.now += adapter._poll_interval


# ── account lookup ──────────────────────────────────────────────────────


async def test_validate_account_resolves_a_username_to_an_account_ref() -> None:
    client = FakeInstagramClient()

    ref = await _make_adapter(client).validate_account("https://www.instagram.com/SomeCreator/")

    assert client.seen_usernames == ["somecreator"]
    assert ref.platform_account_id == "17841400"
    assert ref.username == "somecreator"


async def test_validate_account_raises_permanent_error_when_not_found() -> None:
    client = FakeInstagramClient()
    client.discovery = None

    with pytest.raises(PermanentPlatformError):
        await _make_adapter(client).validate_account("nobody")


async def test_validate_account_rejects_a_blank_identifier() -> None:
    with pytest.raises(PermanentPlatformError):
        await _make_adapter(FakeInstagramClient()).validate_account("  @ ")


async def test_fetch_updates_queries_by_username_not_by_id() -> None:
    client = FakeInstagramClient()

    await _make_adapter(client).fetch_updates(_ACCOUNT, cursor=None)

    assert client.seen_usernames == ["somecreator"]


async def test_fetch_updates_raises_permanent_error_when_account_no_longer_resolves() -> None:
    client = FakeInstagramClient()
    client.discovery = None

    with pytest.raises(PermanentPlatformError):
        await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="m1|2026-10-01T00:00:00+0000")


# ── cursor ──────────────────────────────────────────────────────────────


async def test_first_poll_establishes_a_baseline_without_emitting_events() -> None:
    client = FakeInstagramClient()
    client.discovery["media"] = [_media("m2", "2026-10-02T00:00:00+0000"), _media("m1", "2026-10-01T00:00:00+0000")]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor=None)

    assert result.items == []
    assert result.next_cursor == "m2|2026-10-02T00:00:00+0000"


async def test_only_posts_newer_than_the_cursor_are_returned_oldest_first() -> None:
    client = FakeInstagramClient()
    client.discovery["media"] = [
        _media("m3", "2026-10-03T00:00:00+0000"),
        _media("m2", "2026-10-02T00:00:00+0000"),
        _media("m1", "2026-10-01T00:00:00+0000"),
    ]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="m1|2026-10-01T00:00:00+0000")

    assert [item["id"] for item in result.items] == ["m2", "m3"]
    assert result.next_cursor == "m3|2026-10-03T00:00:00+0000"


async def test_new_items_carry_the_accounts_name_and_profile_picture() -> None:
    client = FakeInstagramClient()
    client.discovery["media"] = [_media("m2", "2026-10-02T00:00:00+0000")]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="m1|2026-10-01T00:00:00+0000")

    assert result.items[0]["_profile"] == {
        "name": "Some Creator",
        "profile_picture_url": "https://cdn.example/avatar.jpg",
    }


async def test_a_deleted_cursor_post_does_not_reannounce_older_posts() -> None:
    """Regression test: the cursor's post being deleted used to make every
    post on the page look new."""
    client = FakeInstagramClient()
    client.discovery["media"] = [_media("m3", "2026-10-03T00:00:00+0000"), _media("m1", "2026-10-01T00:00:00+0000")]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="m2-deleted|2026-10-02T00:00:00+0000")

    assert [item["id"] for item in result.items] == ["m3"]


async def test_a_legacy_bare_id_cursor_still_works() -> None:
    client = FakeInstagramClient()
    client.discovery["media"] = [_media("m2", "2026-10-02T00:00:00+0000"), _media("m1", "2026-10-01T00:00:00+0000")]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="m1")

    assert [item["id"] for item in result.items] == ["m2"]


async def test_a_missing_legacy_cursor_rebaselines_instead_of_flooding() -> None:
    client = FakeInstagramClient()
    client.discovery["media"] = [_media("m2", "2026-10-02T00:00:00+0000"), _media("m1", "2026-10-01T00:00:00+0000")]

    result = await _make_adapter(client).fetch_updates(_ACCOUNT, cursor="gone")

    assert result.items == []
    assert result.next_cursor == "m2|2026-10-02T00:00:00+0000"


async def test_no_media_keeps_the_cursor() -> None:
    result = await _make_adapter(FakeInstagramClient()).fetch_updates(_ACCOUNT, cursor="m1|2026-10-01T00:00:00+0000")

    assert result.items == []
    assert result.next_cursor == "m1|2026-10-01T00:00:00+0000"


# ── sharing, budget, cadence ────────────────────────────────────────────


async def test_two_guilds_on_the_same_account_share_one_lookup_per_tick() -> None:
    client = FakeInstagramClient()
    adapter = _make_adapter(client)
    same_account_other_guild = AccountRef(platform="instagram", platform_account_id="17841400", username="SomeCreator")

    await adapter.fetch_updates(_ACCOUNT, cursor=None)
    await adapter.fetch_updates(same_account_other_guild, cursor=None)

    assert client.seen_usernames == ["somecreator"]


async def test_the_next_tick_looks_up_again() -> None:
    client = FakeInstagramClient()
    clock = Clock()
    adapter = _make_adapter(client, clock)

    await adapter.fetch_updates(_ACCOUNT, cursor=None)
    _next_tick(adapter, clock)
    await adapter.fetch_updates(_ACCOUNT, cursor=None)

    assert len(client.seen_usernames) == 2


async def test_a_used_up_hourly_budget_skips_the_check() -> None:
    client = FakeInstagramClient()
    clock = Clock()
    adapter = _make_adapter(client, clock, hourly_call_limit=2)  # 75% headroom → 1 usable call

    await adapter.fetch_updates(_ACCOUNT, cursor=None)
    _next_tick(adapter, clock)
    result = await adapter.fetch_updates(_ACCOUNT, cursor="m1|2026-10-01T00:00:00+0000")

    assert result.items == [] and result.next_cursor == "m1|2026-10-01T00:00:00+0000"
    assert len(client.seen_usernames) == 1


def test_poll_interval_stretches_with_the_account_count() -> None:
    """One call per account per check against 150 usable calls/hour (75% of
    200): 1 account → 30s floor, 5 → 120s, 10 → 240s."""
    adapter = _make_adapter(FakeInstagramClient())

    assert adapter.poll_interval_seconds(1) == 30.0
    assert adapter.poll_interval_seconds(5) == pytest.approx(120.0)
    assert adapter.poll_interval_seconds(10) == pytest.approx(240.0)


def test_poll_interval_slows_down_when_meta_reports_high_usage() -> None:
    client = FakeInstagramClient()
    adapter = _make_adapter(client)

    client.app_usage_percent = 75
    assert adapter.poll_interval_seconds(5) == pytest.approx(240.0)
    client.app_usage_percent = 95
    assert adapter.poll_interval_seconds(5) == pytest.approx(480.0)


async def test_get_live_status_is_never_expected_to_be_called() -> None:
    with pytest.raises(NotImplementedError):
        await _make_adapter(FakeInstagramClient()).get_live_status(_ACCOUNT)


# ── startup credential check ────────────────────────────────────────────


async def test_a_never_expiring_page_token_with_every_permission_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        await _make_adapter(FakeInstagramClient()).check_credentials()

    assert caplog.records == []


async def test_a_short_lived_token_is_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """The easy mistake: a Page token fetched with the short-lived user
    token expires within hours instead of never."""
    client = FakeInstagramClient()
    client.token_info["expires_at"] = int(time.time()) + 3600

    with caplog.at_level(logging.WARNING):
        await _make_adapter(client).check_credentials()

    assert "Extend Access Token" in caplog.text


async def test_a_missing_permission_is_flagged(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeInstagramClient()
    client.token_info["scopes"] = ["instagram_basic", "pages_read_engagement"]

    with caplog.at_level(logging.WARNING):
        await _make_adapter(client).check_credentials()

    assert "instagram_manage_insights" in caplog.text


async def test_an_invalid_token_is_flagged(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeInstagramClient()
    client.token_info = {"is_valid": False}

    with caplog.at_level(logging.WARNING):
        await _make_adapter(client).check_credentials()

    assert "invalid" in caplog.text
