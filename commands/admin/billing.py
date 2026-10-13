"""`/pulsenotify subscribe` and the bot-owner whitelist commands
(`/pulsenotify global whitelist-add|whitelist-remove|whitelist-list`).

Subscribing is per server (BILLING_PRICE_USD a month): card via Paddle (renews monthly by
itself) or crypto via NOWPayments (3/6/12 months prepaid). The poller in
billing/service.py confirms payments and posts a confirmation in the
channel the payment was started from.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from billing.access import Access, describe_access, parse_time
from billing.service import CRYPTO_COINS, CRYPTO_PLANS, BillingService, BillingUnavailable, format_usd
from commands.pulsenotify_group import global_group, pulsenotify_group
from ui import cards
from utils.errors import PlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

_VIEW_TIMEOUT_SECONDS = 600.0


def _when(moment: Optional[datetime.datetime], style: str) -> str:
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "soon"


def _billing(interaction: discord.Interaction) -> BillingService:
    return interaction.client.billing  # type: ignore[attr-defined]


# ── /pulsenotify subscribe ──────────────────────────────────────────────


@pulsenotify_group.command(name="subscribe", description="Subscribe this server to PulseNotify (monthly, card or crypto).")
@app_commands.checks.has_permissions(manage_guild=True)
async def subscribe(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    billing = _billing(interaction)
    if not billing.enabled:
        await cards.reply(interaction, "PulseNotify is free on this server — no subscription needed.", ephemeral=True)
        return

    access = await billing.access(interaction.guild.id)
    row = await billing.billing_row(interaction.guild.id)
    view = SubscribeView(billing, owner_id=interaction.user.id, access=access, row=row)
    await cards.reply(interaction, view=view, ephemeral=True)


class _OwnedView(discord.ui.LayoutView):
    """Buttons only the person who ran the command can press."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT_SECONDS)
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await cards.error(interaction, "Only the person who ran this command can use these buttons.")
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any, /) -> None:
        await _report_error(interaction, error)


async def _report_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, (BillingUnavailable, PlatformError)):
        message = str(error)
    else:
        logger.error("Billing action failed in guild %s", interaction.guild_id, exc_info=error)
        message = "Something went wrong starting that payment — it's been logged. Please try again."
    try:
        await cards.error(interaction, message)
    except discord.HTTPException:
        pass


class SubscribeView(_OwnedView):
    def __init__(self, billing: BillingService, *, owner_id: int, access: Access, row: Optional[Dict[str, Any]]) -> None:
        super().__init__(owner_id)
        self._billing = billing

        card = discord.ui.Button(
            label=f"Card — {billing.price_label}/month", emoji="💳", style=discord.ButtonStyle.primary,
            disabled=not billing.card_available,
        )
        crypto = discord.ui.Button(
            label="Crypto — 3/6/12 months", emoji="🪙", style=discord.ButtonStyle.primary,
            disabled=not billing.crypto_available,
        )
        card.callback = self.pay_by_card  # type: ignore[method-assign]
        crypto.callback = self.pay_by_crypto  # type: ignore[method-assign]
        buttons = [card, crypto]
        if row and row.get("card_subscription_id") and billing.card_available:
            manage = discord.ui.Button(label="Manage card subscription", style=discord.ButtonStyle.secondary)
            manage.callback = self.manage_card  # type: ignore[method-assign]
            buttons.append(manage)

        lines = [
            describe_access(access, row, price_label=billing.price_label),
            f"**{billing.price_label}/month** per server — every feature, every monitored account.",
            "💳 **Card** renews monthly by itself. Subscribing during the free trial saves your card and "
            "charges nothing until the trial ends. 🪙 **Crypto** is prepaid for 3, 6 or 12 months "
            "(crypto can't renew automatically).",
            "**Cancel any time** with *Manage card subscription*: cancel during the trial and you're never "
            "charged; cancel after paying and the server stays active until the end of the paid month, "
            "with no further charges.",
        ]
        if not billing.card_available or not billing.crypto_available:
            lines.append("-# Buttons that are greyed out aren't set up on this bot yet.")
        self.add_item(cards.container(*lines, title="PulseNotify subscription", extra=[discord.ui.ActionRow(*buttons)]))

    async def pay_by_card(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        trial_days = await self._billing.remaining_trial_days(interaction.guild.id)
        url = await self._billing.start_card_checkout(
            interaction.guild.id, user_id=interaction.user.id, channel_id=interaction.channel_id
        )
        link = discord.ui.Button(label="Subscribe by card", style=discord.ButtonStyle.link, url=url, emoji="💳")
        if trial_days > 0:
            lines = [
                f"Open the secure Paddle checkout to subscribe for **{self._billing.price_label}/month**.",
                f"You're still in the free trial: your card is saved now and the **first "
                f"{self._billing.price_label} charge is in "
                f"{trial_days} day(s)**, when the trial ends. Cancel before then and you pay nothing.",
            ]
        else:
            lines = [
                f"Open the secure Paddle checkout to subscribe for **{self._billing.price_label}/month**.",
                "This server activates within about 2 minutes of paying — I'll post a confirmation in this channel.",
            ]
        await cards.reply(
            interaction,
            *lines,
            title="Card checkout",
            extra=[discord.ui.ActionRow(link)],
            ephemeral=True,
        )

    async def pay_by_crypto(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=CryptoView(self._billing, owner_id=interaction.user.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def manage_card(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = await self._billing.manage_card_url(interaction.guild.id)
        link = discord.ui.Button(label="Open billing portal", style=discord.ButtonStyle.link, url=url)
        await cards.reply(
            interaction,
            "Update your card or cancel in Paddle's billing portal. "
            "If you cancel, the server stays active until the end of the paid month.",
            title="Manage card subscription",
            extra=[discord.ui.ActionRow(link)],
            ephemeral=True,
        )


class CryptoView(_OwnedView):
    def __init__(self, billing: BillingService, *, owner_id: int) -> None:
        super().__init__(owner_id)
        self._billing = billing
        self.months: Optional[int] = None
        self.coin: Optional[str] = None

        months = discord.ui.Select(
            placeholder="How many months?",
            options=[
                discord.SelectOption(label=f"{plan} months — {format_usd(plan * billing.price_usd)}", value=str(plan))
                for plan in CRYPTO_PLANS
            ],
        )
        coin = discord.ui.Select(
            placeholder="Pay with…",
            options=[discord.SelectOption(label=label, value=code) for code, label in CRYPTO_COINS.items()],
        )
        go = discord.ui.Button(label="Get payment address", style=discord.ButtonStyle.success)
        months.callback = self._pick_months  # type: ignore[method-assign]
        coin.callback = self._pick_coin  # type: ignore[method-assign]
        go.callback = self.create_payment  # type: ignore[method-assign]
        self._months_select = months
        self._coin_select = coin
        self.add_item(
            cards.container(
                "Pick how long to prepay and which coin to pay with.",
                "-# Low-fee networks like USDT on TRON or BNB Chain cost the least to send.",
                title="Pay with crypto",
                extra=[discord.ui.ActionRow(months), discord.ui.ActionRow(coin), discord.ui.ActionRow(go)],
            )
        )

    async def _pick_months(self, interaction: discord.Interaction) -> None:
        self.months = int(self._months_select.values[0])
        await interaction.response.defer()

    async def _pick_coin(self, interaction: discord.Interaction) -> None:
        self.coin = self._coin_select.values[0]
        await interaction.response.defer()

    async def create_payment(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if self.months is None or self.coin is None:
            await cards.error(interaction, "Pick the number of months and a coin first.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payment = await self._billing.start_crypto_payment(
            interaction.guild.id,
            months=self.months,
            coin=self.coin,
            user_id=interaction.user.id,
            channel_id=interaction.channel_id,
        )
        await cards.reply(interaction, view=crypto_payment_card(payment, months=self.months, coin=self.coin), ephemeral=True)


def crypto_payment_card(payment: Dict[str, Any], *, months: int, coin: str) -> discord.ui.LayoutView:
    amount = payment.get("pay_amount")
    currency = str(payment.get("pay_currency") or coin).upper()
    lines = [
        f"Send **exactly `{amount}` {currency}** ({CRYPTO_COINS.get(coin, coin)}) to:",
        f"```\n{payment.get('pay_address')}\n```",
    ]
    if payment.get("payin_extra_id"):
        lines.append(f"⚠️ Also include this **memo / tag**: `{payment['payin_extra_id']}` — without it the payment is lost.")
    expires = parse_time(payment.get("expiration_estimate_date"))
    if expires:
        lines.append(f"Send it before {_when(expires, 't')} ({_when(expires, 'R')}).")
    lines.append(
        f"This prepays **{months} months**. Once the network confirms it (usually a few minutes), "
        "this server activates automatically and I'll post a confirmation in this channel."
    )
    return cards.card(*lines, title="Crypto payment", footer=f"Payment ID {payment.get('payment_id')}")


# ── /pulsenotify global whitelist-* (bot owner only) ─────────────────────


async def _owner_only(interaction: discord.Interaction) -> bool:
    if await interaction.client.is_owner(interaction.user):  # type: ignore[attr-defined]
        return True
    await cards.error(interaction, "Only the bot owner can use this command.")
    return False


def _parse_server_id(interaction: discord.Interaction, server_id: Optional[str]) -> Optional[int]:
    if not server_id:
        return interaction.guild_id
    value = server_id.strip()
    return int(value) if value.isdigit() else None


def _server_label(interaction: discord.Interaction, guild_id: int) -> str:
    guild = interaction.client.get_guild(guild_id)
    return f"**{discord.utils.escape_markdown(guild.name)}** (`{guild_id}`)" if guild else f"`{guild_id}`"


@global_group.command(name="whitelist-add", description="Bot owner only: let a server use PulseNotify without paying.")
@app_commands.describe(server_id="Server ID (leave empty for this server)", note="Why — for your own reference")
async def whitelist_add(interaction: discord.Interaction, server_id: Optional[str] = None, note: Optional[str] = None) -> None:
    if not await _owner_only(interaction):
        return
    guild_id = _parse_server_id(interaction, server_id)
    if guild_id is None:
        await cards.error(interaction, "That's not a server ID — it's the long number from *Copy Server ID*.")
        return
    # The billing row references the server's settings row, which this creates if needed.
    await interaction.client.guilds_repo.is_enabled(guild_id)  # type: ignore[attr-defined]
    await _billing(interaction).whitelist(guild_id, by=interaction.user.id, note=note)
    logger.info("Server %s whitelisted by %s (%s).", guild_id, interaction.user.id, note)
    await cards.reply(
        interaction, f"{_server_label(interaction, guild_id)} is whitelisted — no payment needed.",
        title="Whitelisted", tone=cards.SUCCESS, ephemeral=True,
    )


@global_group.command(name="whitelist-remove", description="Bot owner only: make a server pay again.")
@app_commands.describe(server_id="Server ID (leave empty for this server)")
async def whitelist_remove(interaction: discord.Interaction, server_id: Optional[str] = None) -> None:
    if not await _owner_only(interaction):
        return
    guild_id = _parse_server_id(interaction, server_id)
    if guild_id is None:
        await cards.error(interaction, "That's not a server ID — it's the long number from *Copy Server ID*.")
        return
    removed = await _billing(interaction).unwhitelist(guild_id)
    if not removed:
        await cards.error(interaction, f"{_server_label(interaction, guild_id)} isn't whitelisted.")
        return
    logger.info("Server %s removed from the whitelist by %s.", guild_id, interaction.user.id)
    await cards.reply(
        interaction,
        f"{_server_label(interaction, guild_id)} is no longer whitelisted — it needs an active subscription or trial.",
        title="Removed from whitelist", ephemeral=True,
    )


@global_group.command(name="whitelist-list", description="Bot owner only: every whitelisted server.")
async def whitelist_list(interaction: discord.Interaction) -> None:
    if not await _owner_only(interaction):
        return
    rows = await _billing(interaction).list_whitelisted()
    if not rows:
        await cards.reply(interaction, "No servers are whitelisted.", title="Whitelist", ephemeral=True)
        return
    lines = []
    for row in rows:
        note = f" — {discord.utils.escape_markdown(row['whitelist_note'])}" if row.get("whitelist_note") else ""
        lines.append(f"⭐ {_server_label(interaction, int(row['guild_id']))}{note}")
    await cards.reply(interaction, "\n".join(lines), title=f"Whitelist · {len(rows)}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — the commands attach to the shared groups at import time."""
