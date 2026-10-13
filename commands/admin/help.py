"""`/pulsenotify help` — lists every command and what it does.

Built by introspecting `pulsenotify_group.commands` rather than a
hand-maintained list, so it can never drift out of sync with what's
actually registered — add a subcommand anywhere in the group and it shows
up here automatically, with no second place to remember to update.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from commands.pulsenotify_group import pulsenotify_group
from config import constants
from ui import cards


# Hidden from /pulsenotify help for everyone but bot owners (the commands
# themselves refuse non-owners too).
OWNER_ONLY_COMMANDS = {"pulsenotify global status"}


@pulsenotify_group.command(name="help", description="Show all PulseNotify commands and what they do.")
async def help_command(interaction: discord.Interaction) -> None:
    fields = []
    # walk_commands() includes commands inside subcommand groups
    # (e.g. /pulsenotify global status); the groups themselves are skipped.
    commands_ = [c for c in pulsenotify_group.walk_commands() if isinstance(c, app_commands.Command)]
    is_owner = await interaction.client.is_owner(interaction.user)  # type: ignore[attr-defined]
    for command in sorted(commands_, key=lambda c: c.qualified_name):
        if command.name == "help" or (command.qualified_name in OWNER_ONLY_COMMANDS and not is_owner):
            continue
        usage = _format_usage(command)
        value = command.description or "No description."
        if usage:
            value += f"\n`{usage}`"
        fields.append((f"/{_qualified_name(command)}", value))

    # The text-command prefix is configurable (DISCORD_COMMAND_PREFIX), so
    # the hint shows whatever this bot actually answers to.
    prefix = getattr(interaction.client, "command_prefix", constants.DEFAULT_COMMAND_PREFIX)
    if not isinstance(prefix, str):
        prefix = constants.DEFAULT_COMMAND_PREFIX
    await cards.reply(
        interaction,
        "Every command below is a subcommand of `/pulsenotify`.",
        title=f"{constants.BOT_NAME} Commands",
        fields=fields,
        footer=f"{constants.BOT_NAME} v{constants.BOT_VERSION} · {prefix}sync is a text command, not shown above",
        ephemeral=True,
    )


def _format_usage(command: app_commands.Command) -> str:
    tokens = [param.name if param.required else f"[{param.name}]" for param in command.parameters]
    if not tokens:
        return ""
    return f"/{_qualified_name(command)} " + " ".join(tokens)


def _qualified_name(command: app_commands.Command) -> str:
    return getattr(command, "qualified_name", None) or f"pulsenotify {command.name}"


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — `help` attaches to the shared pulsenotify_group at
    import time (the decorator above)."""
