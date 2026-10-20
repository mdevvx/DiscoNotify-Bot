"""`/notify toggle` — enable or disable DiscoNotify for the current server.

Requires Manage Server. The disabled check itself lives in
DiscoNotifyCommandTree.interaction_check (bot/client.py), which blocks every
other command while a guild is disabled but always lets this one through
(matched there by qualified_name "notify toggle").
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from commands.notify_group import notify_group
from config import constants
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)


@notify_group.command(name="toggle", description="Enable or disable DiscoNotify for this server.")
@app_commands.describe(state="Whether DiscoNotify should be turned on or off in this server.")
@app_commands.choices(
    state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def toggle(interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
    assert interaction.guild is not None
    bot = interaction.client

    enabled = state.value == "on"
    await bot.guilds_repo.set_enabled(interaction.guild.id, enabled)  # type: ignore[attr-defined]

    logger.info(
        "Guild %s (%s) %s by %s (%s).",
        interaction.guild.name,
        interaction.guild.id,
        "enabled" if enabled else "disabled",
        interaction.user,
        interaction.user.id,
    )

    if enabled:
        await cards.reply(
            interaction,
            f"{constants.BOT_NAME} has been **enabled** for this server.",
            title="🟢 Enabled",
            tone=cards.SUCCESS,
        )
    else:
        await cards.reply(
            interaction,
            f"{constants.BOT_NAME} has been **disabled** for this server. All monitoring and notifications are "
            "paused. Run `/notify toggle` again to re-enable.",
            title="🔴 Disabled",
            tone=cards.ERROR,
        )


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — `toggle` attaches to the shared notify_group at
    import time (the decorator above)."""
