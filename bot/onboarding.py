"""What happens when DiscoNotify joins a new server: make its slash commands
available there right away, and — when billing is on — DM the admin who added
the bot a link to activate the free trial on the website. Nothing is posted in
the server.

The trial does not start on joining: it starts when someone activates it on
the website (which collects an email address). Until then commands answer
with that link (bot/client.py, DiscoNotifyCommandTree).

Command sync: commands registered *globally* (`!sync global`) already work
in every server, and copying them into a server as well makes Discord show
each one twice there — so the per-server sync only runs when the bot's
commands aren't global.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

import discord

from billing.access import AccessReason
from commands.notify_group import notify_group
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.client import DiscoNotifyBot

logger = get_logger(__name__)

# The audit log entry for "bot added" can show up a moment after the join event.
_INVITER_LOOKUPS = 3
_INVITER_LOOKUP_DELAY_SECONDS = 2.0


async def on_join(bot: "DiscoNotifyBot", guild: discord.Guild) -> None:
    """Every step is independent — one failing never stops the others."""
    try:
        await sync_commands_to(bot, guild)
    except Exception:
        logger.warning("Couldn't sync commands to new server %s.", guild.id, exc_info=True)

    if not bot.billing.enabled:
        return
    try:
        # The billing row references the server's row, so make sure that exists first.
        await bot.guilds_repo.is_enabled(guild.id)
        await bot.guilds_repo.set_present(guild.id, True)
        access = await bot.billing.access(guild.id)
        if access.reason != AccessReason.NEEDS_ACTIVATION:
            return  # already has a trial (the bot was added back), or is whitelisted
        inviter = await find_inviter(guild)
        if inviter is None:
            logger.info("Couldn't tell who added the bot to server %s; no activation DM sent.", guild.id)
            return
        await bot.billing.set_invited_by(guild.id, inviter.id)
        await bot.send_activation_dm(guild, inviter.id)
    except Exception:
        logger.warning("Couldn't set up the trial activation for new server %s.", guild.id, exc_info=True)


async def find_inviter(guild: discord.Guild) -> Optional[Any]:
    """The person who added the bot, from the server's audit log (needs the
    View Audit Log permission in the invite link). None if it can't be told."""
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None
    for attempt in range(_INVITER_LOOKUPS):
        if attempt:
            await asyncio.sleep(_INVITER_LOOKUP_DELAY_SECONDS)
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                target = entry.target
                if target is not None and target.id == me.id and entry.user is not None and not entry.user.bot:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
    return None


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
