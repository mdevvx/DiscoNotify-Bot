"""`/pulsenotify status` — bot health, per-server diagnostics, and platform health.

Only reports what's actually true right now. Notification counts aren't
shown because nothing tracks a global count yet — showing a fabricated
number would be misleading rather than accurate.
"""

from __future__ import annotations

import datetime

import discord
from discord.ext import commands

from commands.pulsenotify_group import pulsenotify_group
from config import constants
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)


@pulsenotify_group.command(name="status", description="Show PulseNotify's current health and status for this server.")
async def status(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert interaction.guild is not None  # enforced by the group's guild_only=True

    latency_ms = round(bot.latency * 1000)
    uptime = _format_uptime(getattr(bot, "started_at", None))
    db_connected = bot.database.is_connected  # type: ignore[attr-defined]
    guild_enabled = await bot.guilds_repo.is_enabled(interaction.guild.id)  # type: ignore[attr-defined]

    overview = " · ".join(
        [
            "🟢 Online",
            f"Uptime **{uptime}**",
            f"Latency **{latency_ms}ms**",
            f"**{len(bot.guilds)}** server(s)",
        ]
    )
    platform_status = await _format_platform_health(bot)
    await cards.reply(
        interaction,
        overview,
        title=f"{constants.BOT_NAME} Status",
        fields=[
            ("Database", "🟢 Connected" if db_connected else "🔴 Disconnected"),
            ("This Server", "🟢 Enabled" if guild_enabled else "🔴 Disabled"),
            ("Platform Monitoring", platform_status),
        ],
        footer=f"{constants.BOT_NAME} v{constants.BOT_VERSION}",
    )


_HEALTH_ICONS = {"healthy": "🟢", "degraded": "🟡", "error": "🔴", "disabled": "⚪"}


async def _format_platform_health(bot: commands.Bot) -> str:
    """Never shows last_error or anything else that isn't meant for a
    public channel — just status/account counts/quota usage. Only reports
    on platforms that actually have an adapter registered; nothing is
    fabricated for the rest.

    Account counts are queried fresh from the database rather than read
    from PlatformScheduler's cached worker count — that cache only
    refreshes once per poll cycle (up to MONITORING_LIVE_POLL_INTERVAL_SECONDS,
    60s by default), so right after /pulsenotify account-add it would
    otherwise still show the pre-add count until the next cycle ran.
    """
    registered = bot.monitoring.registered_platforms  # type: ignore[attr-defined]
    if not registered:
        return "No platforms configured yet."

    health = await bot.monitoring.health_snapshot()  # type: ignore[attr-defined]
    lines = []
    for platform in registered:
        accounts = await bot.accounts_repo.list_enabled_by_platform(platform)  # type: ignore[attr-defined]
        account_count = len(accounts)
        row = health.get(platform)
        icon = _HEALTH_ICONS.get(row["status"], "⚪") if row else "⚪"
        quota_note = ""
        if row is not None:
            # quota_limit/quota_remaining are only set where the provider
            # itself reports them back (none do yet) — requests_today is
            # this bot's own count, always accurate once anything's polled.
            if row.get("quota_limit"):
                quota_note = f" · {row['requests_today']}/{row['quota_limit']} quota units today"
            elif row.get("requests_today"):
                quota_note = f" · {row['requests_today']} API requests today"
        lines.append(f"{icon} **{platform.title()}** — {account_count} account(s) monitored{quota_note}")
    return "\n".join(lines)


def _format_uptime(started_at: "datetime.datetime | None") -> str:
    if started_at is None:
        return "Just started"

    delta = discord.utils.utcnow() - started_at
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — `status` attaches to the shared pulsenotify_group at
    import time (the decorator above); bot/client.py registers that group
    to the tree once, after every command-contributing extension loads."""