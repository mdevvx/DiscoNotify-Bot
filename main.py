"""Entry point: load config, set up logging, construct the bot, run it.

Startup order matters here — configuration must be valid and logging must
be ready before anything else runs, so problems in either surface
immediately instead of being swallowed by a half-initialized bot.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from bot.client import DiscoNotifyBot
from bot.lifecycle import run
from config.settings import describe_settings_errors, get_settings
from utils.logger import get_logger, setup_logging


async def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        # One readable line per bad variable instead of pages of traceback
        # (a typo like BILLING_POLL_INTERVAL_SECONDS=600e crash-looped a deploy).
        sys.exit("DiscoNotify can't start — fix these environment variables (values aren't shown):\n" + describe_settings_errors(exc))
    setup_logging(settings.log_level, settings.log_dir)
    logger = get_logger(__name__)

    logger.info("Starting DiscoNotify (environment=%s)...", settings.environment)

    token = settings.discord_token
    # Fingerprint only (length + ends), to compare against the known-good token.
    logger.info("Discord token loaded: %d chars, %s...%s", len(token), token[:2], token[-2:])

    bot = DiscoNotifyBot(settings=settings)
    await run(bot, settings.discord_token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
