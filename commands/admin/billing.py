"""`/pulsenotify subscribe` and `/pulsenotify cancel` — both send the server
admin to the PulseNotify website, with this server's ID filled in. All
payments and cancellations happen there; the poller in billing/service.py
confirms them with Paddle / NOWPayments and updates the server's access.

(The bot-owner whitelist is the `!whitelist` text command in
commands/admin/whitelist.py, kept out of the slash-command menu on purpose.)
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from billing.access import AccessReason, describe_access, parse_time
from billing.service import BillingService
from commands.pulsenotify_group import pulsenotify_group
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

_NOT_READY = "The payment page isn't available yet — please check back soon."


def _when(moment: Optional[datetime.datetime], style: str) -> str:
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "soon"


def _billing(interaction: discord.Interaction) -> BillingService:
    return interaction.client.billing  # type: ignore[attr-defined]


def subscribe_link(billing: BillingService, guild_id: int, label: str = "Subscribe on the website") -> List[discord.ui.Item]:
    """A link button to the website's payment page for this server, or
    nothing while BILLING_SUBSCRIBE_URL isn't set."""
    url = billing.subscribe_url(guild_id)
    if not url:
        return []
    return [discord.ui.ActionRow(discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url, emoji="⭐"))]


# ── /pulsenotify subscribe ──────────────────────────────────────────────


@pulsenotify_group.command(name="subscribe", description="Subscribe this server to PulseNotify on our website.")
@app_commands.checks.has_permissions(manage_guild=True)
async def subscribe(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    billing = _billing(interaction)
    if not billing.enabled:
        await cards.reply(interaction, "PulseNotify is free on this server — no subscription needed.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    access = await billing.access(guild_id)
    row = await billing.billing_row(guild_id)
    lines = [f"This server: {describe_access(access, row, price_label=billing.price_label)}"]
    if access.reason == AccessReason.WHITELISTED:
        await cards.reply(interaction, *lines, title="PulseNotify subscription", ephemeral=True)
        return

    link = subscribe_link(billing, guild_id)
    lines += [
        f"**{billing.price_label}/month** per server — every feature, every monitored account.",
        "Subscriptions are handled on the **PulseNotify website**: 💳 **card** renews monthly by itself, "
        "🪙 **crypto** is prepaid for 3, 6 or 12 months.",
        "Subscribing during the free trial charges nothing until the trial ends. Cancel any time with "
        "`/pulsenotify cancel`.",
        f"-# This server's ID is `{guild_id}` — the button fills it in for you. "
        "Alerts switch on within about 2 minutes of paying.",
    ]
    if not link:
        lines.append(_NOT_READY)
        logger.warning("BILLING_SUBSCRIBE_URL isn't set — /pulsenotify subscribe has no link to show.")
    await cards.reply(interaction, *lines, title="⭐ PulseNotify subscription", extra=link, ephemeral=True)


# ── /pulsenotify cancel ─────────────────────────────────────────────────


@pulsenotify_group.command(name="cancel", description="Cancel this server's PulseNotify subscription on our website.")
@app_commands.checks.has_permissions(manage_guild=True)
async def cancel(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    billing = _billing(interaction)
    if not billing.enabled:
        await cards.reply(interaction, "PulseNotify is free on this server — there's nothing to cancel.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    access = await billing.access(guild_id)
    row = await billing.billing_row(guild_id) or {}
    if access.reason == AccessReason.WHITELISTED:
        await cards.reply(interaction, "This server is whitelisted — it never pays, so there's nothing to cancel.", ephemeral=True)
        return

    lines = _cancel_lines(billing, access.reason, row)
    url = billing.cancel_url(guild_id)
    extra: List[discord.ui.Item] = []
    if url:
        extra = [
            discord.ui.ActionRow(
                discord.ui.Button(style=discord.ButtonStyle.link, label="Cancel on the website", url=url, emoji="✖️")
            )
        ]
    else:
        lines.append("The cancellation page isn't available yet — please check back soon.")
        logger.warning("BILLING_CANCEL_URL isn't set — /pulsenotify cancel has no link to show.")
    lines.append(f"-# This server's ID is `{guild_id}` — the button fills it in for you.")
    await cards.reply(interaction, *lines, title="Cancel subscription", extra=extra, ephemeral=True)


def _cancel_lines(billing: BillingService, reason: AccessReason, row: Dict[str, Any]) -> List[str]:
    card_status = row.get("card_status")
    cancels_at = parse_time(row.get("card_cancels_at"))
    if card_status in ("active", "trialing", "past_due") and cancels_at is not None:
        return [f"Already cancelled — there are no further charges, and alerts keep running until {_when(cancels_at, 'D')}."]
    if card_status == "trialing":
        return [
            "Your card is saved but **hasn't been charged** — cancel before the free trial ends and you pay nothing.",
        ]
    if card_status in ("active", "past_due"):
        return [
            f"Cancelling stops the monthly {billing.price_label} charge. **No refund is needed** — the server stays "
            "active until the end of the month you've already paid for, then alerts pause (your settings are kept).",
        ]
    if reason in (AccessReason.PAID, AccessReason.GRACE):
        return [
            "This server is prepaid with crypto, which never renews by itself — nothing will be charged. "
            "Alerts keep running until the prepaid time ends.",
        ]
    if reason == AccessReason.TRIAL:
        return ["This server is on the free trial with no payment method saved — nothing will be charged."]
    return ["This server has no active subscription, so nothing will be charged."]


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — the commands attach to the shared groups at import time."""
