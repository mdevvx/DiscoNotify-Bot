"""The PulseNotify Discord client.

Owns the services every command/cog needs (database, repositories,
monitoring, notification delivery) and the startup/shutdown lifecycle.
Registering a new platform adapter is the only thing _register_platform_adapters()
needs to grow for — everything else here is already platform-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from typing import Any, Dict, Optional, Set, cast

import discord
from discord import app_commands
from discord.ext import commands

from billing.access import Access
from billing.nowpayments import NowPaymentsClient
from billing.paddle import PaddleClient
from billing.repository import BillingRepository
from billing.service import BillingService
from bot.events import handle_app_command_error, register_events
from commands.pulsenotify_group import pulsenotify_group
from config.settings import Settings
from database.client import Database
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.channels import NotificationChannelRepository
from database.repositories.guilds import GuildRepository
from monitoring.manager import MonitoringManager
from notifications.sender import NotificationSender
from platforms.instagram.adapter import InstagramAdapter
from platforms.instagram.client import InstagramClient
from platforms.kick.adapter import KickAdapter
from platforms.kick.client import KickClient
from platforms.youtube.adapter import YouTubeAdapter
from platforms.youtube.client import YouTubeClient
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

# Extensions to load at startup. `sync` is a real Cog (a text command, not
# a slash command); the rest are plain modules that decorate their
# subcommand against the shared `pulsenotify_group` at import time — see
# commands/pulsenotify_group.py. New platforms/command groups register here.
_EXTENSIONS = (
    "commands.admin.status",
    "commands.admin.toggle",
    "commands.admin.help",
    "commands.admin.sync",
    "commands.accounts.account_commands",
    "commands.admin.billing",
)


# Work even on a paused server or one without an active subscription:
# toggle re-enables a paused server, status/help explain what's going on,
# subscribe is how a server gets access. Bot-owner `global` commands are
# never gated.
_ALWAYS_ALLOWED = {"pulsenotify toggle", "pulsenotify status", "pulsenotify help", "pulsenotify subscribe"}
_OWNER_GROUP_PREFIX = "pulsenotify global "


class PulseNotifyCommandTree(app_commands.CommandTree):
    """Slash command tree that gates every command behind the guild's
    enabled flag and, when billing is on, an active trial/subscription."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True

        command = interaction.command
        name = command.qualified_name if command is not None else ""
        if name in _ALWAYS_ALLOWED or name.startswith(_OWNER_GROUP_PREFIX):
            return True

        bot = cast("PulseNotifyBot", self.client)
        enabled = await bot.guilds_repo.is_enabled(interaction.guild.id)
        if not enabled:
            await cards.error(
                interaction,
                "PulseNotify is currently disabled in this server. "
                "An administrator can run `/pulsenotify toggle` to re-enable it.",
            )
            return False

        access = await bot.billing.access(interaction.guild.id)
        if not access.allowed:
            await cards.error(
                interaction,
                "This server's PulseNotify trial or subscription has ended, so monitoring is paused. "
                f"An administrator can run `/pulsenotify subscribe` ({bot.billing.price_label}/month) — "
                "all settings are kept.",
            )
            return False

        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await handle_app_command_error(interaction, error)


class PulseNotifyBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to parse prefix commands like `sync`

        super().__init__(
            command_prefix=settings.discord_command_prefix,
            intents=intents,
            tree_cls=PulseNotifyCommandTree,
            help_command=None,
        )

        self.settings = settings
        self.database = Database(settings.supabase_url, settings.supabase_key)
        self.guilds_repo = GuildRepository(self.database)
        self.accounts_repo = PlatformAccountRepository(self.database)
        self.channels_repo = NotificationChannelRepository(self.database)
        self.alert_configs_repo = AlertConfigurationRepository(self.database)

        self.notification_sender = NotificationSender(self, self.alert_configs_repo, self.channels_repo)
        self.monitoring = MonitoringManager(self.database, settings, notification_sink=self.notification_sender.handle)
        self.billing = BillingService(
            BillingRepository(self.database),
            enabled=settings.billing_enabled,
            price_usd=settings.billing_price_usd,
            trial_days=settings.billing_trial_days,
            grace_days=settings.billing_grace_days,
            paddle=PaddleClient(settings.paddle_api_key, sandbox=settings.paddle_sandbox) if settings.paddle_api_key else None,
            paddle_price_id=settings.paddle_price_id,
            nowpayments=(
                NowPaymentsClient(settings.nowpayments_api_key, sandbox=settings.nowpayments_sandbox)
                if settings.nowpayments_api_key
                else None
            ),
        )
        self.billing.on_payment = self._announce_payment
        # Accounts of a paused server, or one without billing access, are
        # simply not polled (their settings stay).
        self.monitoring.guild_filter = self._monitorable_guilds
        self._billing_task: Optional["asyncio.Task[None]"] = None
        self.started_at: Optional[datetime.datetime] = None

        register_events(self)

    async def setup_hook(self) -> None:
        logger.info("Running startup sequence...")

        await self.database.connect()
        logger.info("Connected to Supabase.")

        await self._load_extensions()
        # Every extension above has now decorated its subcommand against
        # the shared pulsenotify_group (see commands/pulsenotify_group.py)
        # — register the fully-populated group to the tree exactly once.
        self.tree.add_command(pulsenotify_group)
        await self._resolve_owners()

        self._register_platform_adapters()
        await self.monitoring.start()
        if self.billing.enabled:
            self._billing_task = asyncio.create_task(self._billing_loop(), name="billing-poller")
            logger.info(
                "Billing enabled (card: %s, crypto: %s).",
                "on" if self.billing.card_available else "not configured",
                "on" if self.billing.crypto_available else "not configured",
            )

        if self.settings.discord_dev_guild_id:
            guild = discord.Object(id=self.settings.discord_dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to dev guild %s.", len(synced), self.settings.discord_dev_guild_id)

        logger.info("Startup sequence complete.")

    async def _load_extensions(self) -> None:
        for extension in _EXTENSIONS:
            try:
                await self.load_extension(extension)
                logger.info("Loaded extension: %s", extension)
            except Exception:
                # One broken extension must not prevent the rest of the bot
                # from starting.
                logger.exception("Failed to load extension: %s", extension)

    def _register_platform_adapters(self) -> None:
        """Registers one adapter per platform with a configured API key.
        A missing key just disables that platform (logged, not fatal) —
        matches the startup flow's "non-critical platform unavailable must
        not stop the bot" rule. Each future platform follows this same
        if-key-present-register pattern.

        Only finalized platforms are registered: YouTube, Kick, Instagram.
        Twitch and X (Twitter) adapters exist under platforms/ but are held
        back until finalized — see the matching note on _PLATFORM_CHOICES
        in commands/accounts/account_commands.py."""
        if self.settings.youtube_api_key:
            client = YouTubeClient(self.settings.youtube_api_key, timeout_seconds=self.settings.monitoring_http_timeout_seconds)
            adapter = YouTubeAdapter(
                client,
                daily_quota_units=self.settings.youtube_daily_quota_units,
                min_poll_interval_seconds=self.settings.youtube_min_poll_interval_seconds,
            )
            self.monitoring.register_adapter(adapter)
        else:
            logger.warning("YOUTUBE_API_KEY not set — YouTube monitoring is disabled.")

        if self.settings.kick_client_id and self.settings.kick_client_secret:
            kick_client = KickClient(
                self.settings.kick_client_id,
                self.settings.kick_client_secret,
                timeout_seconds=self.settings.monitoring_http_timeout_seconds,
            )
            self.monitoring.register_adapter(
                KickAdapter(kick_client, poll_interval_seconds=self.settings.kick_poll_interval_seconds)
            )
        else:
            logger.warning("KICK_CLIENT_ID/KICK_CLIENT_SECRET not set — Kick monitoring is disabled.")

        if self.settings.instagram_access_token and self.settings.instagram_business_account_id:
            instagram_client = InstagramClient(
                self.settings.instagram_access_token,
                self.settings.instagram_business_account_id,
                timeout_seconds=self.settings.monitoring_http_timeout_seconds,
            )
            self.monitoring.register_adapter(
                InstagramAdapter(
                    instagram_client,
                    hourly_call_limit=self.settings.instagram_hourly_call_limit,
                    min_poll_interval_seconds=self.settings.instagram_min_poll_interval_seconds,
                )
            )
        else:
            logger.warning(
                "INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_BUSINESS_ACCOUNT_ID not set — Instagram monitoring is disabled."
            )

    async def _resolve_owners(self) -> None:
        owner_ids = set(self.settings.discord_owner_ids)
        try:
            app_info = await self.application_info()
            if app_info.team:
                owner_ids.update(member.id for member in app_info.team.members)
            elif app_info.owner:
                owner_ids.add(app_info.owner.id)
        except discord.HTTPException:
            logger.warning("Could not fetch application info to resolve bot owners.", exc_info=True)

        self.owner_ids = owner_ids
        logger.info("Resolved %d bot owner(s).", len(owner_ids))

    async def _monitorable_guilds(self, guild_ids: Set[int]) -> Set[int]:
        enabled = {guild_id for guild_id in guild_ids if await self.guilds_repo.is_enabled(guild_id)}
        return await self.billing.allowed_guild_ids(enabled)

    async def _billing_loop(self) -> None:
        while True:
            try:
                await self.billing.poll_once()
            except Exception:
                logger.exception("Billing poll failed.")
            await asyncio.sleep(self.settings.billing_poll_interval_seconds)

    async def _announce_payment(self, payment: Dict[str, Any], access: Access) -> None:
        """Posts "payment received" in the channel the payment was started from."""
        channel_id = payment.get("channel_id")
        if not channel_id:
            return
        channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
        row = await self.billing.billing_row(int(payment["guild_id"])) or {}
        if payment["provider"] == "paddle" and row.get("card_status") == "trialing":
            # Started during the free trial: card saved, nothing charged yet.
            first_charge = f"<t:{int(access.until.timestamp())}:D>" if access.until else "the end of the trial"
            view = cards.card(
                f"Thanks <@{payment['requested_by']}>! Card subscription set up — the first "
                f"{self.billing.price_label} charge is on "
                f"{first_charge}, when the free trial ends.",
                "Cancel before then with `/pulsenotify subscribe` → *Manage card subscription* and you pay nothing.",
                title="✅ Subscription set up",
                tone=cards.SUCCESS,
            )
        else:
            until = f" — active until <t:{int(access.until.timestamp())}:D>" if access.until else ""
            method = "card" if payment["provider"] == "paddle" else "crypto"
            view = cards.card(
                f"Thanks <@{payment['requested_by']}>! This server's PulseNotify subscription is active{until}.",
                f"Paid by {method} · {payment['months']} month(s)",
                title="✅ Payment received",
                tone=cards.SUCCESS,
            )
        await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())  # type: ignore[union-attr]

    async def close(self) -> None:
        logger.info("Shutting down PulseNotify...")
        if self._billing_task is not None:
            self._billing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._billing_task
        await self.billing.aclose()
        await self.monitoring.stop()
        await self.notification_sender.aclose()
        await self.database.close()
        await super().close()
