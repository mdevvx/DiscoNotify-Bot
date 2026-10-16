"""Discord lifecycle events and centralized error handling.

Kept separate from bot/client.py so the client class itself stays focused
on construction and startup/shutdown, not on every individual event
callback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot import onboarding
from config import constants
from ui import cards
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.client import PulseNotifyBot

logger = get_logger(__name__)


def register_events(bot: "PulseNotifyBot") -> None:
    """Attaches the bot's event listeners. Called once from PulseNotifyBot.__init__."""

    @bot.event
    async def on_ready() -> None:
        bot.started_at = discord.utils.utcnow()
        logger.info("%s is online as %s (%d guild(s)).", constants.BOT_NAME, bot.user, len(bot.guilds))
        if bot.maintenance.active:
            # Restarted while in maintenance: show it from the first moment.
            await bot.apply_presence()

    @bot.event
    async def on_guild_join(guild: discord.Guild) -> None:
        logger.info("Joined guild: %s (%s)", guild.name, guild.id)
        # Ensures a row (and warm cache entry) exists for this guild immediately.
        await bot.guilds_repo.is_enabled(guild.id)
        # Commands available right away, trial started, intro posted.
        await onboarding.on_join(bot, guild)

    @bot.event
    async def on_guild_remove(guild: discord.Guild) -> None:
        # Configuration is retained on removal so re-adding the bot restores
        # it, rather than forcing admins to reconfigure from scratch.
        logger.info("Removed from guild: %s (%s). Configuration retained.", guild.name, guild.id)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        await handle_prefix_command_error(ctx, error)


async def handle_prefix_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.NotOwner):
        # Don't confirm to non-owners that an owner-only command exists.
        logger.warning(
            "Unauthorized attempt to use owner-only command '%s' by %s (%s).",
            ctx.command,
            ctx.author,
            ctx.author.id,
        )
        return

    if isinstance(error, commands.CheckFailure):
        return

    logger.error("Unhandled error in prefix command '%s': %s", ctx.command, error, exc_info=error)


async def handle_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down — you can use this again in {max(1, round(error.retry_after))} seconds."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You are not able to use this command right now."
    else:
        logger.error(
            "Unhandled app command error in '%s' (guild=%s): %s",
            getattr(interaction.command, "qualified_name", "unknown"),
            interaction.guild_id,
            error,
            exc_info=error,
        )
        message = "Something went wrong while running that command. The issue has been logged."

    try:
        await cards.error(interaction, message)
    except discord.HTTPException:
        logger.warning("Failed to deliver error message to interaction.", exc_info=True)
