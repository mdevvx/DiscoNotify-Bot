"""What happens when DiscoNotify joins a new server: make its slash commands
available there right away, start its free trial (when billing is on), and
post an intro card with the commands needed to set it up.

Command sync: commands registered *globally* (`!sync global`) already work
in every server, and copying them into a server as well makes Discord show
each one twice there — so the per-server sync only runs when the bot's
commands aren't global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord

from billing.access import Access, AccessReason
from commands.notify_group import notify_group
from config import constants
from ui import cards
from utils.logger import get_logger

if TYPE_CHECKING:
    from bot.client import DiscoNotifyBot

logger = get_logger(__name__)


async def on_join(bot: "DiscoNotifyBot", guild: discord.Guild) -> None:
    """Every step is independent — one failing (e.g. no channel the bot can
    post in) never stops the others."""
    try:
        await sync_commands_to(bot, guild)
    except Exception:
        logger.warning("Couldn't sync commands to new server %s.", guild.id, exc_info=True)

    access: Optional[Access] = None
    try:
        if bot.billing.enabled:
            access = await bot.billing.access(guild.id)  # starts the free trial now
    except Exception:
        logger.warning("Couldn't start the trial for new server %s.", guild.id, exc_info=True)

    channel = pick_intro_channel(guild)
    if channel is None:
        logger.info("No channel to post the intro in for server %s.", guild.id)
        return
    try:
        await channel.send(view=intro_card(bot, access), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.warning("Couldn't post the intro in server %s.", guild.id, exc_info=True)


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


def intro_card(bot: Any, access: Optional[Access]) -> discord.ui.LayoutView:
    fields = [
        (
            "1. Add an account",
            "`/notify account-add` → pick the platform, enter the channel or username, and choose where "
            "alerts post. *(Manage Server)*",
        ),
        (
            "2. Tune the alerts (optional)",
            "`/notify alerts` — which alert types are on · `/notify account-set-mention` — who gets "
            "pinged · `/notify account-set-message` — your own message",
        ),
        ("3. Check it", "`/notify status` — everything monitored here · `/notify help` — every command"),
    ]
    if access is not None and access.reason == AccessReason.TRIAL and access.until is not None:
        fields.append(
            (
                "Free trial",
                f"Your {bot.settings.billing_trial_days}-day free trial has started (ends "
                f"<t:{int(access.until.timestamp())}:D>). Keep it running with `/notify subscribe` — "
                f"{bot.billing.price_label}/month, card or crypto.",
            )
        )
    return cards.card(
        f"{constants.BOT_NAME} posts an alert in your server the moment a creator uploads, goes live or posts — "
        "**YouTube** (videos, Shorts, live streams), **Kick** (live streams), **Instagram** (posts, Reels), **Twitter / X** (posts) and **TikTok** (videos).",
        title=f"👋 Thanks for adding {constants.BOT_NAME}!",
        fields=fields,
        footer="In alert channels the bot needs View Channel, Send Messages and Manage Webhooks",
    )
