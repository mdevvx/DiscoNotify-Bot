"""Process-level startup/shutdown orchestration.

Discord connection lifecycle (login, gateway connect, per-guild readiness)
is handled by discord.py and DiscoNotifyBot.setup_hook(). What lives here is
the layer above that: running the bot until something asks it to stop
(Ctrl+C locally, SIGTERM in a container) and making sure `bot.close()` —
which also closes the Supabase connection — always runs before the process
exits, instead of tasks being torn down mid-flight.
"""

from __future__ import annotations

import asyncio
import signal

from bot.client import DiscoNotifyBot
from utils.logger import get_logger

logger = get_logger(__name__)


async def run(bot: DiscoNotifyBot, token: str) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(sig_name: str) -> None:
        logger.info("Received %s, shutting down gracefully...", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # Not supported on Windows; Ctrl+C falls back to KeyboardInterrupt,
            # which the finally block below still handles correctly.
            pass

    async with bot:
        bot_task = asyncio.create_task(bot.start(token))
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not bot.is_closed():
                await bot.close()
            if not bot_task.done():
                bot_task.cancel()
            if not stop_task.done():
                stop_task.cancel()

        if bot_task.done() and not bot_task.cancelled() and bot_task.exception():
            raise bot_task.exception()  # type: ignore[misc]
