"""_format_platform_health: what /status shows for platform monitoring —
must never fabricate data for unregistered platforms or unset quota
fields (section 14: no sensitive/fabricated internal info), and must
reflect the database's current state rather than a stale scheduler cache
(a real bug: right after /notify account-add, the scheduler's own
worker-count cache doesn't refresh until its next poll cycle, up to 60s
later — /status must not show a stale pre-add count in that window)."""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from commands.admin.status import _format_platform_health


class FakeAccountsRepo:
    def __init__(self, counts: Dict[str, int]) -> None:
        self._counts = counts

    async def list_enabled_by_platform(self, platform: str) -> List[dict]:
        return [{"id": str(i)} for i in range(self._counts.get(platform, 0))]


class FakeMonitoring:
    def __init__(self, registered_platform_names, health: Dict[str, Optional[dict]]) -> None:
        # The real registered_platforms is {name: worker_count}, but
        # _format_platform_health only reads the keys now (see its
        # docstring) — the fake still exposes it as a dict for shape
        # fidelity, with throwaway values to make that explicit.
        self.registered_platforms = {name: -1 for name in registered_platform_names}
        self._health = health

    async def health_snapshot(self):
        return self._health


class FakeBot:
    def __init__(self, monitoring: FakeMonitoring, account_counts: Dict[str, int]) -> None:
        self.monitoring = monitoring
        self.accounts_repo = FakeAccountsRepo(account_counts)


async def test_no_platforms_registered() -> None:
    bot = FakeBot(FakeMonitoring([], {}), {})
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "No platforms" in text


async def test_account_count_comes_from_the_database_not_a_cache() -> None:
    """The exact scenario from the bug report: an account was just added,
    the scheduler's own cache hasn't refreshed yet, but /status must still
    show the real current count."""
    bot = FakeBot(FakeMonitoring(["youtube"], {"youtube": None}), {"youtube": 1})
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "1 account(s)" in text


async def test_registered_platform_with_no_health_row_yet() -> None:
    bot = FakeBot(FakeMonitoring(["youtube"], {"youtube": None}), {"youtube": 2})
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "Youtube" in text
    assert "2 account(s)" in text


async def test_healthy_platform_shows_green_icon() -> None:
    bot = FakeBot(
        FakeMonitoring(["youtube"], {"youtube": {"status": "healthy", "requests_today": 0, "quota_limit": None}}),
        {"youtube": 1},
    )
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "🟢" in text


async def test_error_platform_shows_red_icon() -> None:
    bot = FakeBot(
        FakeMonitoring(["youtube"], {"youtube": {"status": "error", "requests_today": 0, "quota_limit": None}}),
        {"youtube": 1},
    )
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "🔴" in text


async def test_shows_raw_request_count_when_no_quota_limit_known() -> None:
    bot = FakeBot(
        FakeMonitoring(["youtube"], {"youtube": {"status": "healthy", "requests_today": 42, "quota_limit": None}}),
        {"youtube": 1},
    )
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "42 API requests today" in text


async def test_shows_ratio_when_quota_limit_is_known() -> None:
    bot = FakeBot(
        FakeMonitoring(["youtube"], {"youtube": {"status": "healthy", "requests_today": 42, "quota_limit": 10_000}}),
        {"youtube": 1},
    )
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "42/10000 quota units today" in text


async def test_zero_requests_today_shows_no_quota_note() -> None:
    bot = FakeBot(
        FakeMonitoring(["youtube"], {"youtube": {"status": "healthy", "requests_today": 0, "quota_limit": None}}),
        {"youtube": 1},
    )
    text = await _format_platform_health(bot)  # type: ignore[arg-type]
    assert "requests today" not in text


async def test_shows_what_is_left_when_the_bot_reports_its_remaining_quota() -> None:
    health = {
        "youtube": {"status": "healthy", "requests_today": 42, "quota_limit": 10_000, "quota_remaining": 9_500},
        "instagram": {"status": "healthy", "requests_today": 9, "quota_limit": 200, "quota_remaining": 170},
    }
    bot = FakeBot(FakeMonitoring(["youtube", "instagram"], health), {"youtube": 1, "instagram": 1})

    text = await _format_platform_health(bot)  # type: ignore[arg-type]

    assert "9,500 of 10,000 quota left today" in text
    assert "170 of 200 quota left this hour" in text
