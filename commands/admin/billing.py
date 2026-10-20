"""`/notify subscribe` and `/notify cancel`.

Card payments go through Polar (billing/polar.py): `subscribe` creates a
hosted checkout for this server and `cancel` opens Polar's customer portal,
where the subscription is cancelled or the card changed. Crypto is paid on
the website (BILLING_SUBSCRIBE_URL, 3/6/12 months prepaid). The poller in
billing/service.py confirms payments with Polar / NOWPayments and updates
the server's access.

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
from billing.service import BillingService, BillingUnavailable
from commands.notify_group import notify_group
from ui import cards
from utils.logger import get_logger
from utils.user_messages import friendly_platform_error

logger = get_logger(__name__)

_NOT_READY = "Payments aren't switched on yet — please check back soon."
_LIVE_CARD_STATUSES = ("active", "trialing", "past_due")


def _when(moment: Optional[datetime.datetime], style: str) -> str:
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "soon"


def _billing(interaction: discord.Interaction) -> BillingService:
    return interaction.client.billing  # type: ignore[attr-defined]


def card_is_live(row: Optional[Dict[str, Any]]) -> bool:
    """The server has a card subscription that's running (not ended)."""
    row = row or {}
    return bool(row.get("card_customer_id")) and row.get("card_status") in _LIVE_CARD_STATUSES


def _link(label: str, url: str, emoji: str) -> discord.ui.Button:
    return discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url, emoji=emoji)


async def subscribe_buttons(
    billing: BillingService, guild_id: int, guild_name: Optional[str] = None
) -> List[discord.ui.Item]:
    """Link buttons for subscribing this server — the website's subscribe page
    when subscribing happens there, otherwise a fresh card checkout and the
    website's crypto page. For messages sent without a command to report
    errors to (the reminder DMs): a failure just leaves that button out."""
    website_url = await billing.website_subscribe_url(guild_id, guild_name=guild_name)
    if website_url:
        return [discord.ui.ActionRow(_link(f"Subscribe on the website — {billing.price_label}/month", website_url, "⭐"))]
    buttons: List[discord.ui.Button] = []
    if billing.card_available:
        try:
            buttons.append(
                _link(f"Subscribe by card — {billing.price_label}/month", await billing.start_card_checkout(guild_id), "💳")
            )
        except Exception:
            logger.warning("Couldn't create a card checkout for server %s.", guild_id, exc_info=True)
    crypto_url = billing.subscribe_url(guild_id)
    if crypto_url:
        buttons.append(_link("Pay with crypto — 3/6/12 months", crypto_url, "🪙"))
    return [discord.ui.ActionRow(*buttons)] if buttons else []


async def _report_error(interaction: discord.Interaction, error: Exception) -> None:
    """Plain English for everyone — the bot's owner included. The technical
    detail goes to the log under a reference code (utils/user_messages.py)."""
    if isinstance(error, BillingUnavailable):
        message = str(error)  # written for members
    else:
        message = friendly_platform_error(
            error,
            platform="The card payment service",
            context="A billing action",
            not_found="We couldn't complete that. Please try again, or contact support if it keeps happening.",
        )
    try:
        await cards.error(interaction, message)
    except discord.HTTPException:
        pass


# ── /notify subscribe ──────────────────────────────────────────────


@notify_group.command(name="subscribe", description="Subscribe this server to DiscoNotify (monthly, card or crypto).")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.checks.cooldown(3, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def subscribe(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    billing = _billing(interaction)
    if not billing.enabled:
        await cards.reply(interaction, "DiscoNotify is free on this server — no subscription needed.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    access = await billing.access(guild_id)
    row = await billing.billing_row(guild_id)
    lines = [f"This server: {describe_access(access, row, price_label=billing.price_label)}"]
    if access.reason == AccessReason.WHITELISTED:
        await cards.reply(interaction, *lines, title="DiscoNotify subscription", ephemeral=True)
        return

    lines.append(f"**{billing.price_label}/month** per server — every feature and platform.")
    buttons: List[discord.ui.Button] = []
    already_subscribed = card_is_live(row) and not (row or {}).get("card_cancels_at")
    website_url = None if already_subscribed else await billing.website_subscribe_url(
        guild_id,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
        guild_name=interaction.guild.name,
    )
    if already_subscribed:
        lines.append("💳 A card subscription is already set up — change the card or cancel it with `/notify cancel`.")
    elif website_url:
        buttons.append(_link(f"Subscribe on the website — {billing.price_label}/month", website_url, "⭐"))
        lines.append(
            "Subscribing happens on the **DiscoNotify website**: choose 💳 **card** (renews monthly by itself) or "
            "🪙 **crypto** (prepaid for 3, 6 or 12 months), pay, and the site shows you the result right away. "
            "During the free trial your card is saved but **nothing is charged until the trial ends** — cancel "
            "before then with `/notify cancel` and you pay nothing."
        )
        lines.append("-# The link is personal to this server and works for 30 minutes — run this command again for a new one.")
    elif billing.card_available:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            url = await billing.start_card_checkout(
                guild_id, user_id=interaction.user.id, channel_id=interaction.channel_id
            )
        except Exception as exc:
            await _report_error(interaction, exc)
            return
        buttons.append(_link(f"Subscribe by card — {billing.price_label}/month", url, "💳"))
        lines.append(
            "💳 **Card** renews monthly by itself. During the free trial your card is saved but **nothing is "
            "charged until the trial ends** — cancel before then with `/notify cancel` and you pay nothing."
        )

    crypto_url = None if (website_url or already_subscribed) else billing.subscribe_url(guild_id)
    if crypto_url:
        buttons.append(_link("Pay with crypto — 3/6/12 months", crypto_url, "🪙"))
        lines.append("🪙 **Crypto** is prepaid for 3, 6 or 12 months (crypto can't renew automatically).")

    if not buttons and not already_subscribed:
        lines.append(_NOT_READY)
        logger.warning("Neither card (POLAR_*) nor crypto (BILLING_SUBSCRIBE_URL) payments are configured.")
    else:
        lines.append(f"-# This server's ID is `{guild_id}`. Alerts switch on within about 2 minutes of paying.")
    await cards.reply(
        interaction,
        *lines,
        title="⭐ DiscoNotify subscription",
        extra=[discord.ui.ActionRow(*buttons)] if buttons else (),
        ephemeral=True,
    )


# ── /notify cancel ─────────────────────────────────────────────────


@notify_group.command(name="cancel", description="Cancel this server's DiscoNotify subscription or change its card.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.checks.cooldown(3, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def cancel(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    billing = _billing(interaction)
    if not billing.enabled:
        await cards.reply(interaction, "DiscoNotify is free on this server — there's nothing to cancel.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    access = await billing.access(guild_id)
    row = await billing.billing_row(guild_id) or {}
    if access.reason == AccessReason.WHITELISTED:
        await cards.reply(interaction, "This server is whitelisted — it never pays, so there's nothing to cancel.", ephemeral=True)
        return

    lines = _cancel_lines(billing, access.reason, row)
    buttons: List[discord.ui.Button] = []
    if card_is_live(row):
        owner_id = getattr(interaction.guild, "owner_id", None)
        if await billing.can_manage_card(guild_id, interaction.user.id, server_owner_id=owner_id):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                url = await billing.manage_card_url(guild_id)
            except Exception as exc:
                await _report_error(interaction, exc)
                return
            buttons.append(_link("Cancel or change card", url, "⚙️"))
            lines.append("-# The link opens Polar's billing portal and works for about an hour. Don't share it.")
        else:
            payer = await billing.card_payer_id(guild_id)
            who = f"<@{payer}> (who subscribed) or the server owner" if payer else "the server owner"
            lines.append(
                f"🔒 The billing portal shows the payer's email and card details, so only {who} can open it. "
                "Ask them to run `/notify cancel`."
            )
    await cards.reply(
        interaction,
        *lines,
        title="Cancel subscription",
        extra=[discord.ui.ActionRow(*buttons)] if buttons else (),
        ephemeral=True,
    )


def _cancel_lines(billing: BillingService, reason: AccessReason, row: Dict[str, Any]) -> List[str]:
    card_status = row.get("card_status")
    cancels_at = parse_time(row.get("card_cancels_at"))
    if card_status in _LIVE_CARD_STATUSES and cancels_at is not None:
        return [f"Already cancelled — there are no further charges, and alerts keep running until {_when(cancels_at, 'D')}."]
    if card_status == "trialing":
        return ["Your card is saved but **hasn't been charged** — cancel before the free trial ends and you pay nothing."]
    if card_status in ("active", "past_due"):
        return [
            f"Cancelling stops the monthly {billing.price_label} charge. The server stays active until the end of the "
            "month you've already paid for, then alerts pause (your settings are kept).",
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
