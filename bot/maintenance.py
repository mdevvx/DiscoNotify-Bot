"""Maintenance mode: pauses DiscoNotify everywhere — every server's
commands (bot owners excepted, so they can test) and all monitoring — and
shows "🛠️ Under maintenance" as the bot's status.

Stored in the database (notify_bot_state, migration 0007), so it
survives restarts: switch it on, update the bot, restart, check things as
the owner, switch it off. Routine updates don't need it — a restart is a
few seconds and nothing is lost (content cursors and live states are
persisted).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from database.repositories.bot_state import BotStateRepository
from utils.logger import get_logger

logger = get_logger(__name__)

_KEY = "maintenance"  # {message, by, since} — what the bot shows members
_FLAG_KEY = "maintenance_mode"  # plain true/false — the admin panel's switch
DEFAULT_MESSAGE = "DiscoNotify is being updated and will be back shortly."


class Maintenance:
    def __init__(self, repo: BotStateRepository) -> None:
        self._repo = repo
        self.state: Optional[Dict[str, Any]] = None

    @property
    def active(self) -> bool:
        return self.state is not None

    @property
    def message(self) -> str:
        return (self.state or {}).get("message") or DEFAULT_MESSAGE

    async def load(self) -> None:
        """Reads the saved state at startup. A missing table (migration 0007
        not run yet) just means "not in maintenance"."""
        try:
            self.state = await self._repo.get(_KEY)
        except Exception:
            logger.warning("Couldn't read maintenance state (run migration 0007?); assuming off.", exc_info=True)
            self.state = None
        await self.refresh()
        if self.state:
            logger.warning("Starting in maintenance mode: %s", self.message)

    async def refresh(self) -> bool:
        """Syncs with the admin panel's switch (`maintenance_mode`, a plain
        true/false): on turns maintenance on, an explicit off turns it off.
        A missing row or a read failure changes nothing. Returns whether
        maintenance flipped, so the caller can update the bot's status."""
        try:
            flag = await self._repo.get(_FLAG_KEY)
        except Exception:
            logger.warning("Couldn't read the maintenance switch; keeping the current state.", exc_info=True)
            return False
        if flag is True and self.state is None:
            self.state = {"message": DEFAULT_MESSAGE, "by": None, "since": _now()}
            await self._repo.set(_KEY, self.state)
            logger.warning("Maintenance mode ON (admin panel).")
            return True
        if flag is False and self.state is not None:
            self.state = None
            await self._repo.delete(_KEY)
            logger.warning("Maintenance mode OFF (admin panel).")
            return True
        return False

    async def enable(self, *, message: Optional[str], by: int) -> None:
        state = {"message": message or DEFAULT_MESSAGE, "by": by, "since": _now()}
        await self._repo.set(_KEY, state)
        await self._repo.set(_FLAG_KEY, True)  # so the panel shows it too
        self.state = state
        logger.warning("Maintenance mode ON by %s: %s", by, state["message"])

    async def disable(self) -> None:
        await self._repo.delete(_KEY)
        await self._repo.set(_FLAG_KEY, False)
        self.state = None
        logger.warning("Maintenance mode OFF.")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
