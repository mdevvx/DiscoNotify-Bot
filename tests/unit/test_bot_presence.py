"""Which servers the bot is in right now (notify_guilds.bot_present) —
what the website's "pick your server" list relies on."""

from __future__ import annotations

from typing import Any, Dict, List

from bot.events import register_events
from database.repositories.guilds import GuildRepository
from tests.unit.fakes import FakeDatabase


def _present(db: FakeDatabase) -> Dict[int, bool]:
    return {int(row["guild_id"]): row["bot_present"] for row in db.tables["notify_guilds"].rows}


async def test_joining_and_leaving_flip_the_flag_and_keep_the_row() -> None:
    repo = GuildRepository(FakeDatabase())  # type: ignore[arg-type]

    await repo.set_present(1, True)
    await repo.set_present(1, False)

    assert _present(repo._db) == {1: False}  # type: ignore[attr-defined]  # settings stay for when the bot comes back
    await repo.set_present(1, True)
    assert _present(repo._db) == {1: True}  # type: ignore[attr-defined]


async def test_startup_sync_marks_current_servers_present_and_others_absent() -> None:
    db = FakeDatabase()
    repo = GuildRepository(db)  # type: ignore[arg-type]
    await repo.set_present(1, True)  # still in
    await repo.set_present(2, True)  # removed while the bot was offline
    await repo.set_present(3, False)  # re-added while it was offline

    await repo.sync_presence([1, 3, 4])  # 4 was never seen

    assert _present(db) == {1: True, 2: False, 3: True, 4: True}


class _Bot:
    def __init__(self, db: FakeDatabase) -> None:
        self.guilds_repo = GuildRepository(db)  # type: ignore[arg-type]
        self.handlers: Dict[str, Any] = {}
        self.guilds: List[Any] = []
        self.user = "DiscoNotify"
        self.maintenance = type("M", (), {"active": False})()
        self.billing = None

    def event(self, handler: Any) -> Any:
        self.handlers[handler.__name__] = handler
        return handler


async def test_the_bot_keeps_the_flag_up_to_date_from_discord_events() -> None:
    db = FakeDatabase()
    bot = _Bot(db)
    register_events(bot)  # type: ignore[arg-type]
    server = type("G", (), {"id": 5, "name": "Hub"})()
    bot.guilds = [server]
    await bot.guilds_repo.set_present(9, True)  # a server the bot has since left

    await bot.handlers["on_ready"]()
    assert _present(db) == {5: True, 9: False}

    await bot.handlers["on_guild_remove"](server)
    assert _present(db)[5] is False

    await bot.handlers["on_guild_available"](server)
    assert _present(db)[5] is True
