"""`/pulsenotify status` and `/pulsenotify global status`.

- `/pulsenotify status` — this server only: whether monitoring is on, and
  for every account it monitors: the account (linked), where its alerts
  post, how many alert types are on, who gets pinged, whether it's live,
  when it was last checked and when its last alert went out.
- `/pulsenotify global status` — the bot as a whole: uptime, latency,
  server count, database, and every platform's health.

Only reports what's actually true right now — nothing is fabricated for
platforms that aren't configured, and no counts are invented.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Sequence

import discord
from discord.ext import commands

from billing.access import subscription_details
from commands.pulsenotify_group import global_group, pulsenotify_group
from config import constants
from database.repositories.events import EventRepository
from database.repositories.live_states import LiveStateRepository
from notifications.templates import format_role_mention
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

_HEALTH_ICONS = {"healthy": "🟢", "degraded": "🟡", "error": "🔴", "disabled": "⚪"}
_ACCOUNT_ICONS = {"active": "🟢", "error": "🟡", "invalid": "🔴"}
_PLATFORM_NAMES = {"youtube": "YouTube", "kick": "Kick", "instagram": "Instagram", "twitter": "Twitter / X"}
_PLATFORM_LABELS = {"youtube": "▶️ YouTube", "kick": "🟩 Kick", "instagram": "📸 Instagram", "twitter": "𝕏 Twitter / X"}
_PLATFORM_ORDER = ["youtube", "kick", "instagram", "twitter"]
_MAX_ACCOUNTS_PER_PLATFORM = 15
# Discord's limit is 4,000 characters of text per message; leave headroom.
_TEXT_BUDGET = 3800
_MORE_LINE_ROOM = 80  # kept free for each platform's "…and N more" line
_MAX_ERROR_LENGTH = 120


# ── /pulsenotify status (this server) ───────────────────────────────────


@pulsenotify_group.command(
    name="status", description="Show what PulseNotify monitors in this server and how each account is doing."
)
async def status(interaction: discord.Interaction) -> None:
    bot = interaction.client
    guild = interaction.guild
    assert guild is not None  # enforced by the group's guild_only=True

    enabled = await bot.guilds_repo.is_enabled(guild.id)  # type: ignore[attr-defined]
    accounts = await bot.accounts_repo.list_by_guild(guild.id)  # type: ignore[attr-defined]
    channels_by_account = await bot.channels_repo.list_by_account_for_guild(guild.id)  # type: ignore[attr-defined]
    configs = await bot.alert_configs_repo.list_for_guild(guild.id)  # type: ignore[attr-defined]
    live_states = await LiveStateRepository(bot.database).list_for_guild(guild.id)  # type: ignore[attr-defined]
    last_alerts = await EventRepository(bot.database).last_notified_by_account(guild.id)  # type: ignore[attr-defined]
    intervals = bot.monitoring.poll_intervals  # type: ignore[attr-defined]

    configs_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for config in configs:
        configs_by_account.setdefault(config["account_id"], []).append(config)

    channel_ids = {channel["channel_id"] for rows in channels_by_account.values() for channel in rows}
    state = "🟢 Monitoring **on**" if enabled else "🔴 Monitoring **paused** — `/pulsenotify toggle` to resume"
    overview = f"{state} · **{len(accounts)}** account(s) · **{len(channel_ids)}** alert channel(s)"
    billing = bot.billing  # type: ignore[attr-defined]
    fields = []
    has_access = True
    if billing.enabled:
        access = await billing.access(guild.id)
        has_access = access.allowed
        lines = subscription_details(access, await billing.billing_row(guild.id), price_label=billing.price_label)
        fields.append(("💳 Subscription", "\n".join(lines)))

    # Discord rejects a message over 4,000 characters, so account lines are
    # added only while they fit (25 accounts with long error notes came to
    # ~7,500); the rest are counted and pointed to /pulsenotify account-list.
    budget = _TEXT_BUDGET - len(overview) - sum(len(name) + len(value) for name, value in fields)
    budget -= len(guild.name) + len(_format_intervals(intervals, accounts)) + 100
    for platform in _ordered_platforms(accounts):
        rows = [row for row in accounts if row["platform"] == platform]
        lines: List[str] = []
        for row in rows[:_MAX_ACCOUNTS_PER_PLATFORM]:
            line = format_account_line(
                row,
                guild_id=guild.id,
                channels=channels_by_account.get(row["id"], []),
                configs=configs_by_account.get(row["id"], []),
                live_state=live_states.get(row["id"]),
                last_alert_at=last_alerts.get(row["id"]),
            )
            if len(line) + 1 > budget - _MORE_LINE_ROOM:
                break
            lines.append(line)
            budget -= len(line) + 1
        if len(rows) > len(lines):
            more = f"-# …and {len(rows) - len(lines)} more — `/pulsenotify account-list`"
            lines.append(more)
            budget -= len(more) + 1
        label = _PLATFORM_LABELS.get(platform, platform.title())
        heading = f"{label} · {len(rows)}"
        budget -= len(heading)
        fields.append((heading, "\n".join(lines)))

    body = [overview]
    if not accounts:
        body.append("No accounts monitored yet — add one with `/pulsenotify account-add`.")

    await cards.reply(
        interaction,
        *body,
        title=f"{guild.name} — {constants.BOT_NAME} status",
        # Red when something needs attention: paused here, or no subscription.
        tone=cards.SUCCESS if enabled and has_access else cards.WARNING,
        fields=fields,
        footer=_format_intervals(intervals, accounts),
    )


def format_account_line(
    row: Dict[str, Any],
    *,
    guild_id: int,
    channels: Sequence[Dict[str, Any]],
    configs: Sequence[Dict[str, Any]],
    live_state: Optional[Dict[str, Any]],
    last_alert_at: Optional[str],
) -> str:
    """Two lines for one monitored account, e.g.
    🟢 [@lifeafterwinning](https://…) → #alerts · 4/4 alerts on · pings @everyone
    -# 🔴 live since 2h ago · checked 1m ago · last alert 3h ago"""
    icon = "⏸️" if not row.get("enabled", True) else _ACCOUNT_ICONS.get(row.get("status", ""), "⚪")
    name = discord.utils.escape_markdown(row["username"])
    url = account_url(row["platform"], row["platform_account_id"], row["username"])
    head = f"{icon} [{name}]({url})" if url else f"{icon} **{name}**"
    if channels:
        head += " → " + ", ".join(
            f"<#{channel['channel_id']}>" if channel.get("is_valid", True) else f"⚠️ <#{channel['channel_id']}>"
            for channel in channels
        )
    else:
        head += " → ⚠️ no alert channel"
    parts = [head]

    if configs:
        on = sum(1 for config in configs if config.get("enabled"))
        parts.append(f"{on}/{len(configs)} alerts on")
        roles = {config.get("mention_role_id") for config in configs if config.get("enabled")}
        roles.discard(None)
        if len(roles) == 1:
            parts.append(f"pings {format_role_mention(roles.pop(), guild_id)}")
        elif len(roles) > 1:
            parts.append("pings vary by alert type")

    details = []
    if not row.get("enabled", True):
        details.append("paused")
    if live_state and live_state.get("is_live"):
        details.append(f"🔴 **live** since {_relative(live_state.get('started_at'))}")
    checked = _relative(row.get("last_checked_at"))
    details.append(f"checked {checked}" if checked else "not checked yet")
    if last_alert_at:
        details.append(f"last alert {_relative(last_alert_at)}")
    # The raw error (status codes, API text) stays in the database and the
    # log; members get one plain sentence.
    if row.get("status") == "invalid":
        details.append("⚠️ can't be found any more — remove it and add it again")
    elif row.get("status") == "error":
        details.append("⚠️ couldn't be checked just now — retrying automatically")

    return " · ".join(parts) + "\n-# " + " · ".join(details)


def account_url(platform: str, platform_account_id: str, username: str) -> Optional[str]:
    handle = username.lstrip("@")
    if platform == "youtube":
        return f"https://www.youtube.com/channel/{platform_account_id}"
    if platform == "kick":
        return f"https://kick.com/{handle}"
    if platform == "instagram":
        return f"https://www.instagram.com/{handle}/"
    if platform == "twitter":
        return f"https://x.com/{handle}"
    return None


def _ordered_platforms(accounts: Sequence[Dict[str, Any]]) -> List[str]:
    present = {row["platform"] for row in accounts}
    ordered = [platform for platform in _PLATFORM_ORDER if platform in present]
    return ordered + sorted(present - set(ordered))


def _format_intervals(intervals: Dict[str, float], accounts: Sequence[Dict[str, Any]]) -> str:
    platforms = [platform for platform in _ordered_platforms(accounts) if platform in intervals]
    if not platforms:
        return f"{constants.BOT_NAME} v{constants.BOT_VERSION}"
    checks = " · ".join(
        f"{_PLATFORM_NAMES.get(platform, platform.title())} every {round(intervals[platform])}s" for platform in platforms
    )
    return f"Checks: {checks}"


def _relative(iso_time: Optional[str]) -> Optional[str]:
    """<t:…:R> — Discord renders it as "3 minutes ago" in each viewer's locale."""
    if not iso_time:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return f"<t:{int(moment.timestamp())}:R>"


# ── /pulsenotify global status ──────────────────────────────────────────


@global_group.command(name="status", description="Bot owner only: PulseNotify's health across every server.")
async def global_status(interaction: discord.Interaction) -> None:
    bot = interaction.client
    # Bot owners: the Discord application's owner/team plus DISCORD_OWNER_IDS
    # (resolved at startup, the same check !sync uses).
    if not await bot.is_owner(interaction.user):  # type: ignore[attr-defined]
        await cards.error(interaction, "Only the bot owner can use this command.")
        return

    latency_ms = round(bot.latency * 1000)
    uptime = _format_uptime(getattr(bot, "started_at", None))
    db_connected = bot.database.is_connected  # type: ignore[attr-defined]

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
        title=f"{constants.BOT_NAME} Global Status",
        fields=[
            ("Database", "🟢 Connected" if db_connected else "🔴 Disconnected"),
            ("Platform Monitoring", platform_status),
        ],
        footer=f"{constants.BOT_NAME} v{constants.BOT_VERSION} · /pulsenotify status shows this server only",
        ephemeral=True,
    )


async def _format_platform_health(bot: commands.Bot) -> str:
    """Never shows last_error or anything else that isn't meant for a
    public channel — just status/account counts/quota usage. Only reports
    on platforms that actually have an adapter registered; nothing is
    fabricated for the rest.

    Account counts are queried fresh from the database rather than read
    from PlatformScheduler's cached worker count — that cache only
    refreshes once per poll cycle, so right after /pulsenotify account-add
    it would otherwise still show the pre-add count until the next cycle ran.
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
    """No Cog needed — both commands attach to the shared pulsenotify_group
    at import time (the decorators above); bot/client.py registers that
    group to the tree once, after every command-contributing extension loads."""
