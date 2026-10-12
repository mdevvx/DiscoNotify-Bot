"""Poll-interval selection: MonitoringManager's generic per-capability
default, and an adapter's own PlatformAdapter.poll_interval_seconds()
overriding it per cycle (see platforms/base.py's docstring for why a
quota-limited platform needs that).
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from config.settings import Settings
from monitoring.manager import MonitoringManager
from tests.unit.fakes import FakeDatabase
from tests.unit.test_worker import FakeAdapter, _seed_account


class QuotaAwareAdapter(FakeAdapter):
    """Wants 100s per distinct account, like a daily-quota platform."""

    def __init__(self) -> None:
        super().__init__()
        self.asked_with: List[int] = []

    def poll_interval_seconds(self, distinct_account_count: int) -> Optional[float]:
        self.asked_with.append(distinct_account_count)
        return 100.0 * max(distinct_account_count, 1)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")
    monkeypatch.setenv("MONITORING_LIVE_POLL_INTERVAL_SECONDS", "60")
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def test_live_status_adapter_uses_the_generic_live_interval_by_default(settings: Settings) -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    manager = MonitoringManager(db, settings)  # type: ignore[arg-type]
    manager.register_adapter(FakeAdapter())
    scheduler = manager._schedulers["fake"]

    await scheduler._sync_workers()
    scheduler._update_poll_interval()

    assert scheduler._poll_interval == 60


async def test_an_adapter_interval_overrides_the_generic_one(settings: Settings) -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    manager = MonitoringManager(db, settings)  # type: ignore[arg-type]
    manager.register_adapter(QuotaAwareAdapter())
    scheduler = manager._schedulers["fake"]

    await scheduler._sync_workers()
    scheduler._update_poll_interval()

    assert scheduler._poll_interval == 100.0


async def test_adapter_interval_counts_distinct_platform_accounts_not_rows(settings: Settings) -> None:
    """Two guilds watching the same creator are two rows but one channel to
    check — only distinct platform accounts cost quota."""
    db = FakeDatabase()
    _seed_account(db, "acc-1", platform_account_id="UCsame")
    _seed_account(db, "acc-2", guild_id=2, platform_account_id="UCsame")
    _seed_account(db, "acc-3", platform_account_id="UCother")
    adapter = QuotaAwareAdapter()
    manager = MonitoringManager(db, settings)  # type: ignore[arg-type]
    manager.register_adapter(adapter)
    scheduler = manager._schedulers["fake"]

    await scheduler._sync_workers()
    scheduler._update_poll_interval()

    assert adapter.asked_with[-1] == 2
    assert scheduler._poll_interval == 200.0


async def test_adapter_interval_follows_accounts_being_added(settings: Settings) -> None:
    db = FakeDatabase()
    _seed_account(db, "acc-1")
    manager = MonitoringManager(db, settings)  # type: ignore[arg-type]
    manager.register_adapter(QuotaAwareAdapter())
    scheduler = manager._schedulers["fake"]
    await scheduler._sync_workers()
    scheduler._update_poll_interval()

    _seed_account(db, "acc-2")
    await scheduler._sync_workers()
    scheduler._update_poll_interval()

    assert scheduler._poll_interval == 200.0
