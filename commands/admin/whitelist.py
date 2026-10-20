"""`whitelist` — bot-owner-only text command: let a server use DiscoNotify
without paying.

A text command (like `sync`), not a slash command, on purpose: Discord
can't hide a slash subcommand from individual users, so a slash version
would show up in everyone's command menu. As a text command it's invisible
— never in the menu, never in `/notify help` — and a non-owner
typing it gets no reply at all (bot/events.py ignores NotOwner silently).

    !whitelist add [server_id] [note…]   (server_id defaults to this server)
    !whitelist remove [server_id]
    !whitelist list
"""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands

from billing.service import BillingService
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)


def _parse_server_id(ctx: commands.Context, server_id: Optional[str]) -> Optional[int]:
    if not server_id:
        return ctx.guild.id if ctx.guild else None
    value = server_id.strip()
    return int(value) if value.isdigit() else None


class WhitelistCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _billing(self) -> BillingService:
        return self.bot.billing  # type: ignore[attr-defined]

    def _label(self, guild_id: int) -> str:
        guild = self.bot.get_guild(guild_id)
        return f"**{discord.utils.escape_markdown(guild.name)}** (`{guild_id}`)" if guild else f"`{guild_id}`"

    async def _usage(self, ctx: commands.Context) -> None:
        prefix = ctx.clean_prefix
        await ctx.send(
            view=cards.card(
                f"`{prefix}whitelist add [server_id] [note]` — no payment needed (server_id defaults to this server)",
                f"`{prefix}whitelist remove [server_id]` — needs a subscription or trial again",
                f"`{prefix}whitelist list` — every whitelisted server",
                title="Whitelist",
            )
        )

    @commands.group(name="whitelist", invoke_without_command=True)
    @commands.is_owner()
    async def whitelist(self, ctx: commands.Context) -> None:
        await self._usage(ctx)

    @whitelist.command(name="add")
    @commands.is_owner()
    async def add(self, ctx: commands.Context, server_id: Optional[str] = None, *, note: Optional[str] = None) -> None:
        # "!whitelist add my friend's server" — no ID given, the words are the note.
        if server_id and not server_id.strip().isdigit():
            note = f"{server_id} {note}".strip() if note else server_id
            server_id = None
        guild_id = _parse_server_id(ctx, server_id)
        if guild_id is None:
            await ctx.send(view=cards.card("Give a server ID, or run this inside the server.", tone=cards.ERROR))
            return
        # The billing row references the server's settings row, which this creates if needed.
        await self.bot.guilds_repo.is_enabled(guild_id)  # type: ignore[attr-defined]
        await self._billing.whitelist(guild_id, by=ctx.author.id, note=note)
        logger.info("Server %s whitelisted by %s (%s).", guild_id, ctx.author.id, note)
        await ctx.send(
            view=cards.card(f"{self._label(guild_id)} is whitelisted — no payment needed.", title="Whitelisted", tone=cards.SUCCESS)
        )

    @whitelist.command(name="remove")
    @commands.is_owner()
    async def remove(self, ctx: commands.Context, server_id: Optional[str] = None) -> None:
        guild_id = _parse_server_id(ctx, server_id)
        if guild_id is None:
            await ctx.send(view=cards.card("That's not a server ID — it's the long number from *Copy Server ID*.", tone=cards.ERROR))
            return
        if not await self._billing.unwhitelist(guild_id):
            await ctx.send(view=cards.card(f"{self._label(guild_id)} isn't whitelisted.", tone=cards.ERROR))
            return
        logger.info("Server %s removed from the whitelist by %s.", guild_id, ctx.author.id)
        await ctx.send(
            view=cards.card(
                f"{self._label(guild_id)} is no longer whitelisted — it needs an active subscription or trial.",
                title="Removed from whitelist",
            )
        )

    @whitelist.command(name="list")
    @commands.is_owner()
    async def list_(self, ctx: commands.Context) -> None:
        rows = await self._billing.list_whitelisted()
        if not rows:
            await ctx.send(view=cards.card("No servers are whitelisted.", title="Whitelist"))
            return
        lines = []
        for row in rows:
            note = f" — {discord.utils.escape_markdown(row['whitelist_note'])}" if row.get("whitelist_note") else ""
            lines.append(f"⭐ {self._label(int(row['guild_id']))}{note}")
        await ctx.send(view=cards.card("\n".join(lines), title=f"Whitelist · {len(rows)}"))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WhitelistCog(bot))
