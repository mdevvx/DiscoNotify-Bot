"""`/notify help` — every command, grouped by what people want to do,
one short line each, with the subscription highlighted at the top.

The groups and short descriptions are written for readability, but the
command list itself still comes from the registered commands: anything
not placed in a group below is listed under "More" with its own
description, so help can't silently miss a command.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from billing.access import describe_access
from commands.notify_group import notify_group
from config import constants
from ui import cards

# Every `/notify global …` command is bot-owner only: hidden from
# /notify help for everyone else (the commands refuse non-owners too).
OWNER_ONLY_PREFIX = "notify global "

# (section title, [(command name under /notify, one-line description)])
_SECTIONS: Sequence[Tuple[str, Sequence[Tuple[str, str]]]] = (
    (
        "🚀 Get started",
        (
            ("account-add", "Start monitoring a YouTube, Kick, Instagram, Twitter / X or TikTok account"),
            ("status", "What's monitored in this server, and your subscription"),
        ),
    ),
    (
        "🔔 Alerts",
        (
            ("alerts", "Turn alert types and their embeds on or off"),
            ("account-set-mention", "Choose who gets pinged — a role, @everyone or nobody"),
            ("account-set-message", "Write your own alert message"),
        ),
    ),
    (
        "📋 Accounts",
        (
            ("account-list", "Every account monitored here"),
            ("account-info", "One account's full alert settings"),
            ("account-remove", "Stop monitoring an account"),
        ),
    ),
    ("⚙️ Server", (("toggle", "Pause or resume DiscoNotify in this server"),)),
)

# Shown with 🛡️ — they need the Manage Server permission.
_ADMIN_COMMANDS = {
    "account-add",
    "account-remove",
    "account-set-message",
    "account-set-mention",
    "alerts",
    "toggle",
    "subscribe",
    "cancel",
}
_HIDDEN = {"help", "subscribe", "cancel"}  # these have their own box at the top


@notify_group.command(name="help", description="Show all DiscoNotify commands and what they do.")
async def help_command(interaction: discord.Interaction) -> None:
    bot = interaction.client
    registered = {
        command.qualified_name: command
        for command in notify_group.walk_commands()
        if isinstance(command, app_commands.Command)
    }
    is_owner = await bot.is_owner(interaction.user)  # type: ignore[attr-defined]

    view = discord.ui.LayoutView(timeout=None)
    subscribe_box = await _subscribe_box(interaction)
    if subscribe_box is not None:
        view.add_item(subscribe_box)

    blocks: List[str] = ["Every command starts with `/notify`. 🛡️ = needs the **Manage Server** permission."]
    listed = set()
    for title, entries in _SECTIONS:
        lines = []
        for name, description in entries:
            qualified = f"notify {name}"
            if qualified not in registered:
                continue
            listed.add(qualified)
            lines.append(_line(name, description))
        if lines:
            blocks.append(f"**{title}**\n" + "\n".join(lines))

    extra_lines = []
    for qualified, command in sorted(registered.items()):
        name = qualified.removeprefix("notify ")
        if qualified in listed or name in _HIDDEN or qualified.startswith(OWNER_ONLY_PREFIX):
            continue
        extra_lines.append(_line(name, command.description or ""))
    if extra_lines:
        blocks.append("**➕ More**\n" + "\n".join(extra_lines))

    if is_owner:
        blocks.append(_owner_section(bot, registered))

    view.add_item(
        cards.container(
            *blocks,
            title=f"📖 {constants.BOT_NAME} help",
            footer=f"{constants.BOT_NAME} v{constants.BOT_VERSION} · disconotify.com",
        )
    )
    await cards.reply(interaction, view=view, ephemeral=True)


def _line(name: str, description: str) -> str:
    shield = " 🛡️" if name in _ADMIN_COMMANDS else ""
    return f"`/notify {name}`{shield} — {description}"


async def _subscribe_box(interaction: discord.Interaction) -> Optional[discord.ui.Container]:
    """The highlighted box at the top — only while billing is switched on."""
    billing = getattr(interaction.client, "billing", None)
    if billing is None or not billing.enabled or interaction.guild is None:
        return None
    access = await billing.access(interaction.guild.id)
    row = await billing.billing_row(interaction.guild.id)
    return cards.container(
        f"**`/notify subscribe`** 🛡️ — keep alerts running in this server: **{billing.price_label}/month**, "
        "card or crypto.",
        "`/notify cancel` 🛡️ — cancel any time; paid time is never cut short.",
        f"This server: {describe_access(access, row, price_label=billing.price_label)}",
        title="⭐ Subscription",
        tone=cards.WARNING if not access.allowed else cards.INFO,
    )


def _owner_section(bot: discord.Client, registered: Dict[str, app_commands.Command]) -> str:
    prefix = getattr(bot, "command_prefix", constants.DEFAULT_COMMAND_PREFIX)
    if not isinstance(prefix, str):
        prefix = constants.DEFAULT_COMMAND_PREFIX
    lines = [
        f"`/{qualified}` — {command.description}"
        for qualified, command in sorted(registered.items())
        if qualified.startswith(OWNER_ONLY_PREFIX)
    ]
    lines += [
        f"`{prefix}sync [global]` — sync slash commands",
        f"`{prefix}whitelist add|remove|list` — servers that don't pay",
        f"`{prefix}maintenance on|off` — pause DiscoNotify everywhere",
    ]
    return "**👑 Owner only** *(only you see this)*\n" + "\n".join(lines)


# Kept for the usage-string tests and any caller that wants one.
def _format_usage(command: app_commands.Command) -> str:
    tokens = [param.name if param.required else f"[{param.name}]" for param in command.parameters]
    if not tokens:
        return ""
    return f"/{_qualified_name(command)} " + " ".join(tokens)


def _qualified_name(command: app_commands.Command) -> str:
    return getattr(command, "qualified_name", None) or f"notify {command.name}"


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — `help` attaches to the shared notify_group at
    import time (the decorator above)."""
