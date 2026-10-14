"""`maintenance` — bot-owner-only text command (like `sync`/`whitelist`, so
it never shows in anyone's command menu; non-owners get no reply).

    !maintenance                 — current state
    !maintenance on [message]    — pause PulseNotify everywhere (asks to confirm)
    !maintenance off             — resume

See bot/maintenance.py for what "paused" means and why it survives restarts.
"""

from __future__ import annotations

from typing import Any, Optional

import discord
from discord.ext import commands

from bot.maintenance import DEFAULT_MESSAGE
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

_CONFIRM_TIMEOUT_SECONDS = 60.0


class ConfirmMaintenanceView(discord.ui.LayoutView):
    def __init__(self, bot: Any, *, owner_id: int, message: Optional[str]) -> None:
        super().__init__(timeout=_CONFIRM_TIMEOUT_SECONDS)
        self._bot = bot
        self._owner_id = owner_id
        self._message = message or DEFAULT_MESSAGE
        confirm: discord.ui.Button = discord.ui.Button(label="Pause everywhere", style=discord.ButtonStyle.danger)
        cancel: discord.ui.Button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm  # type: ignore[method-assign]
        cancel.callback = self.cancel  # type: ignore[method-assign]
        self.add_item(
            cards.container(
                "This pauses PulseNotify in **every server**: all commands (except yours) and all alerts, "
                "until you run `!maintenance off`. It stays on across restarts.",
                f"Members will see: *{discord.utils.escape_markdown(self._message)}*",
                title="🛠️ Turn on maintenance mode?",
                tone=cards.WARNING,
                extra=[discord.ui.ActionRow(confirm, cancel)],
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    async def confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self._bot.maintenance.enable(message=self._message, by=interaction.user.id)
        await self._bot.apply_presence()
        await interaction.response.edit_message(
            view=cards.card(
                "PulseNotify is paused everywhere. Update and restart as needed, then run `!maintenance off`.",
                title="🛠️ Maintenance mode is on",
                tone=cards.WARNING,
            )
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=cards.card("Cancelled — PulseNotify keeps running.", title="Maintenance"))


class MaintenanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="maintenance", invoke_without_command=True)
    @commands.is_owner()
    async def maintenance(self, ctx: commands.Context) -> None:
        state = self.bot.maintenance  # type: ignore[attr-defined]
        prefix = ctx.clean_prefix
        if state.active:
            await ctx.send(
                view=cards.card(
                    f"**On** since <t:{_timestamp(state.state.get('since'))}:R>. Members see: *{state.message}*",
                    f"`{prefix}maintenance off` to resume.",
                    title="🛠️ Maintenance",
                    tone=cards.WARNING,
                )
            )
        else:
            await ctx.send(
                view=cards.card(
                    "**Off** — PulseNotify is running normally.",
                    f"`{prefix}maintenance on [message]` pauses it everywhere (asks to confirm first).",
                    title="Maintenance",
                )
            )

    @maintenance.command(name="on")
    @commands.is_owner()
    async def on(self, ctx: commands.Context, *, message: Optional[str] = None) -> None:
        if self.bot.maintenance.active:  # type: ignore[attr-defined]
            await ctx.send(view=cards.card("Maintenance mode is already on.", tone=cards.WARNING))
            return
        await ctx.send(view=ConfirmMaintenanceView(self.bot, owner_id=ctx.author.id, message=message))

    @maintenance.command(name="off")
    @commands.is_owner()
    async def off(self, ctx: commands.Context) -> None:
        if not self.bot.maintenance.active:  # type: ignore[attr-defined]
            await ctx.send(view=cards.card("Maintenance mode is already off.", title="Maintenance"))
            return
        await self.bot.maintenance.disable()  # type: ignore[attr-defined]
        await self.bot.apply_presence()  # type: ignore[attr-defined]
        await ctx.send(
            view=cards.card(
                "PulseNotify is running again in every server — alerts resume on the next check.",
                title="✅ Maintenance mode is off",
            )
        )


def _timestamp(iso: Optional[str]) -> int:
    import datetime

    try:
        return int(datetime.datetime.fromisoformat(str(iso)).timestamp())
    except (TypeError, ValueError):
        return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MaintenanceCog(bot))
