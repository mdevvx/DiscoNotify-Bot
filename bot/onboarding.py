"""What happens when DiscoNotify joins a new server: make its slash commands
available there right away and start its free trial (when billing is on).
Nothing is posted in the server.

Command sync: commands registered *globally* (`!sync global`) already work
in every server, and copying them into a server as well makes Discord show
each one twice there — so the per-server sync only runs when the bot's
commands aren't global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord

from commands.notify_group import notify_group
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.client import DiscoNotifyBot

logger = get_logger(__name__)


async def on_join(bot: "DiscoNotifyBot", guild: discord.Guild) -> None:
    """Make the slash commands available in the new server right away and
    start its free trial (when billing is on). Nothing is posted in the
    server. Each step is independent — one failing never stops the other."""
    try:
        await sync_commands_to(bot, guild)
    except Exception:
        logger.warning("Couldn't sync commands to new server %s.", guild.id, exc_info=True)

    try:
        if bot.billing.enabled:
            await bot.billing.access(guild.id)  # starts the free trial now
    except Exception:
        logger.warning("Couldn't start the trial for new server %s.", guild.id, exc_info=True)


async def sync_commands_to(bot: Any, guild: discord.abc.Snowflake) -> str:
    """"global" if the commands are already global (nothing to do),
    otherwise copies them into this server — visible immediately."""
    global_commands = await bot.tree.fetch_commands()
    if any(command.name == notify_group.name for command in global_commands):
        return "global"
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    logger.info("Synced %d command(s) to new server %s.", len(synced), guild.id)
    return "synced"


def pick_intro_channel(guild: discord.Guild) -> Optional[Any]:
    """The server's system channel (where Discord posts join messages) if
    the bot can post there, otherwise the first text channel it can."""
    me = guild.me
    candidates = [guild.system_channel] if guild.system_channel is not None else []
    candidates += sorted(guild.text_channels, key=lambda channel: channel.position)
    for channel in candidates:
        permissions = channel.permissions_for(me)
        if permissions.view_channel and permissions.send_messages:
            return channel
    return None
