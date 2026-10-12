"""`sync` — owner-only application command synchronization.

A text command rather than a slash command deliberately: if the command
tree is out of sync, slash commands may not even be registered yet, so
syncing needs a channel that doesn't depend on them. Invoked with whatever
prefix is configured (DISCORD_COMMAND_PREFIX, e.g. `!`):

    !sync         -> copies + syncs to the current guild (instant, for dev)
    !sync global  -> syncs globally (production; can take up to an hour)
"""

from __future__ import annotations

from discord.ext import commands

from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)


class SyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        scope = scope.lower()

        if scope == "global":
            synced = await self.bot.tree.sync()
            logger.info("Global command sync by %s (%s): %d command(s).", ctx.author, ctx.author.id, len(synced))
            await ctx.send(
                view=cards.card(
                    f"Synced {len(synced)} command(s) globally. This can take up to an hour to reach every server.",
                    title="Commands synced",
                    tone=cards.SUCCESS,
                )
            )
            return

        if scope == "guild":
            if ctx.guild is None:
                await ctx.send(view=cards.card("Run this inside a server to sync commands to that server.", tone=cards.ERROR))
                return
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            logger.info(
                "Guild command sync by %s (%s) in %s (%s): %d command(s).",
                ctx.author,
                ctx.author.id,
                ctx.guild.name,
                ctx.guild.id,
                len(synced),
            )
            await ctx.send(
                view=cards.card(
                    f"Synced {len(synced)} command(s) to this server. Changes are visible immediately.",
                    title="Commands synced",
                    tone=cards.SUCCESS,
                )
            )
            return

        prefix = ctx.clean_prefix
        await ctx.send(
            view=cards.card(
                f"`{prefix}sync` syncs to this server; `{prefix}sync global` syncs everywhere.",
                title="Usage",
                tone=cards.ERROR,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SyncCog(bot))
