"""The DiscoNotify Discord client.

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

from billing.access import Access, AccessReason, activation_needed, parse_time, subscription_needed
from bot.maintenance import Maintenance
from billing.nowpayments import NowPaymentsClient
from billing.polar import PolarClient
from billing.repository import BillingRepository
from billing.service import BillingEvent, BillingService
from bot.events import handle_app_command_error, register_events
from commands.notify_group import notify_group
from config.settings import Settings
from database.client import Database
from database.repositories.accounts import PlatformAccountRepository
from database.repositories.alert_configs import AlertConfigurationRepository
from database.repositories.bot_state import BotStateRepository
from database.repositories.channels import NotificationChannelRepository
from database.repositories.dm_log import DmLogRepository
from database.repositories.guilds import GuildRepository
from mail.repository import MailRepository
from mail.service import EmailService
from mail.smtp import SmtpClient
from monitoring.manager import MonitoringManager
from notifications.sender import NotificationSender
from platforms.instagram.adapter import InstagramAdapter
from platforms.instagram.client import InstagramClient
from platforms.kick.adapter import KickAdapter
from platforms.kick.client import KickClient
from platforms.tiktok.adapter import TikTokAdapter
from platforms.tiktok.client import TikTokClient
from platforms.twitter.adapter import TwitterAdapter
from platforms.twitter.client import TwitterClient
from platforms.youtube.adapter import YouTubeAdapter
from platforms.youtube.client import YouTubeClient
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

# Extensions to load at startup. `sync` is a real Cog (a text command, not
# a slash command); the rest are plain modules that decorate their
# subcommand against the shared `notify_group` at import time — see
# commands/notify_group.py. New platforms/command groups register here.
_EXTENSIONS = (
    "commands.admin.status",
    "commands.admin.toggle",
    "commands.admin.help",
    "commands.admin.sync",
    "commands.accounts.account_commands",
    "commands.admin.billing",
    "commands.admin.whitelist",
    "commands.admin.maintenance",
)


# Work even on a paused server or one without an active subscription:
# toggle re-enables a paused server, status/help explain what's going on,
# subscribe/cancel link to the website. Bot-owner `global` commands are
# never gated.
_ALWAYS_ALLOWED = {
    "notify toggle",
    "notify status",
    "notify help",
    "notify subscribe",
    "notify cancel",
}
# How often the scheduled emails (trial-ended and payment-coming-up reminders)
# are looked for, and the follow-up activation DMs.
_REMINDER_CHECK_EVERY = datetime.timedelta(minutes=10)
# A new server that still hasn't activated its trial this long after the bot
# joined gets one follow-up DM (never more), but only while it's still recent.
_ACTIVATION_FOLLOWUP_AFTER = datetime.timedelta(hours=24)
_ACTIVATION_FOLLOWUP_WINDOW = datetime.timedelta(days=7)
_OWNER_GROUP_PREFIX = "notify global "
# Still answered for a server that hasn't activated its trial (they explain what to do).
_ACTIVATION_EXEMPT = {"notify help", "notify status"}
# Discord error "Cannot send messages to this user".
_DM_CLOSED = 50007
# Old DM-log rows are purged after this many days (checked about once a day).
_DM_LOG_RETENTION_DAYS = 90


class DiscoNotifyCommandTree(app_commands.CommandTree):
    """Slash command tree that gates every command behind the guild's
    enabled flag and, when billing is on, an active trial/subscription."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True

        bot = cast("DiscoNotifyBot", self.client)
        if bot.maintenance.active and not await bot.is_owner(interaction.user):
            await cards.reply(
                interaction,
                bot.maintenance.message,
                "Your settings are safe, and alerts resume automatically when it's over.",
                title="🛠️ DiscoNotify is under maintenance",
                tone=cards.WARNING,
                ephemeral=True,
            )
            return False

        command = interaction.command
        name = command.qualified_name if command is not None else ""
        if name.startswith(_OWNER_GROUP_PREFIX):
            return True

        # A new server's free trial starts when it's activated on the website:
        # until then every command (except help and status, which explain it)
        # just points there.
        if name not in _ACTIVATION_EXEMPT:
            activation = await bot.billing.access(interaction.guild.id)
            if activation.reason == AccessReason.NEEDS_ACTIVATION:
                url = bot.billing.activate_url
                button = discord.ui.Button(style=discord.ButtonStyle.link, label="Activate free trial", url=url, emoji="🎁")
                await cards.reply(
                    interaction,
                    *activation_needed(url),
                    title="🕐 Activate your free trial",
                    tone=cards.INFO,
                    ephemeral=True,
                    extra=[discord.ui.ActionRow(button)],
                )
                return False

        if name in _ALWAYS_ALLOWED:
            return True

        enabled = await bot.guilds_repo.is_enabled(interaction.guild.id)
        if not enabled:
            await cards.error(
                interaction,
                "DiscoNotify is currently disabled in this server. "
                "An administrator can run `/notify toggle` to re-enable it.",
            )
            return False

        access = await bot.billing.access(interaction.guild.id)
        if not access.allowed:
            row = await bot.billing.billing_row(interaction.guild.id)
            await cards.reply(
                interaction,
                *subscription_needed(row, price_label=bot.billing.price_label),
                title="🔒 Subscription needed",
                tone=cards.WARNING,
                ephemeral=True,
            )
            return False

        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await handle_app_command_error(interaction, error)


class DiscoNotifyBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # required to parse prefix commands like `sync`

        super().__init__(
            command_prefix=settings.discord_command_prefix,
            intents=intents,
            tree_cls=DiscoNotifyCommandTree,
            help_command=None,
            # Shown under the bot's name, e.g. "/notify help | disconotify.com".
            # Set at login, so it's there from the first moment the bot is online.
            activity=discord.CustomActivity(name=settings.discord_status_text) if settings.discord_status_text else None,
            status=discord.Status.online,
        )

        self.settings = settings
        self.database = Database(settings.supabase_url, settings.supabase_key)
        self.guilds_repo = GuildRepository(self.database)
        self.accounts_repo = PlatformAccountRepository(self.database)
        self.channels_repo = NotificationChannelRepository(self.database)
        self.alert_configs_repo = AlertConfigurationRepository(self.database)
        self.maintenance = Maintenance(BotStateRepository(self.database))
        self.dm_log = DmLogRepository(self.database)

        self.notification_sender = NotificationSender(self, self.alert_configs_repo, self.channels_repo)
        self.monitoring = MonitoringManager(self.database, settings, notification_sink=self.notification_sender.handle)
        self.billing = BillingService(
            BillingRepository(self.database),
            enabled=settings.billing_enabled,
            price_usd=settings.billing_price_usd,
            trial_days=settings.billing_trial_days,
            grace_days=settings.billing_grace_days,
            subscribe_url=settings.billing_subscribe_url,
            activate_url=settings.billing_activate_url,
            link_secret=settings.billing_link_secret.get_secret_value() if settings.billing_link_secret else None,
            link_ttl_minutes=settings.billing_link_ttl_minutes,
            reminder_every_days=settings.billing_reminder_every_days,
            reminder_max=settings.billing_reminder_max,
            polar=(
                PolarClient(settings.polar_access_token, sandbox=settings.polar_sandbox)
                if settings.polar_access_token
                else None
            ),
            polar_product_id=settings.polar_product_id,
            polar_success_url=settings.polar_success_url,
            nowpayments=(
                NowPaymentsClient(settings.nowpayments_api_key, sandbox=settings.nowpayments_sandbox)
                if settings.nowpayments_api_key
                else None
            ),
        )
        self.mail = EmailService(
            MailRepository(self.database),
            BotStateRepository(self.database),
            client=(
                SmtpClient(
                    settings.email_smtp_host,  # type: ignore[arg-type]
                    settings.email_smtp_port,
                    settings.email_smtp_username,  # type: ignore[arg-type]
                    settings.email_smtp_password.get_secret_value(),  # type: ignore[union-attr]
                    sender=settings.email_from,  # type: ignore[arg-type]
                    security=settings.email_smtp_mode,
                )
                if settings.email_configured
                else None
            ),
            link_secret=settings.billing_link_secret.get_secret_value() if settings.billing_link_secret else None,
            unsubscribe_url=settings.email_unsubscribe_url or self.billing.site_url("/unsubscribe"),
            subscribe_url=settings.billing_subscribe_url or self.billing.site_url("/subscribe"),
            activate_url=self.billing.activate_url,
            manage_url=self.billing.site_url("/manage"),
            support_email=settings.support_email or "",
            price_label=self.billing.price_label,
            trial_days=settings.billing_trial_days,
            reminder_every_days=settings.billing_reminder_every_days,
            reminder_max=settings.billing_reminder_max,
            renewal_days=settings.email_renewal_reminder_days,
        )
        self.billing.on_payment = self._announce_payment
        self.billing.on_event = self._announce_billing_event
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
        await self.maintenance.load()

        await self._load_extensions()
        # Every extension above has now decorated its subcommand against
        # the shared notify_group (see commands/notify_group.py)
        # — register the fully-populated group to the tree exactly once.
        self.tree.add_command(notify_group)
        await self._resolve_owners()

        self._register_platform_adapters()
        await self.monitoring.start()
        if self.billing.enabled:
            self._billing_task = asyncio.create_task(self._billing_loop(), name="billing-poller")
            await self.billing.check_setup()
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

        Only finalized platforms are registered: YouTube, Kick, Instagram, X, TikTok.
        The Twitch adapter exists under platforms/ but is held
        back until finalized — see the matching note on _PLATFORM_CHOICES
        in commands/accounts/account_commands.py."""
        if self.settings.youtube_api_key:
            client = YouTubeClient(self.settings.youtube_api_key, timeout_seconds=self.settings.monitoring_http_timeout_seconds)
            adapter = YouTubeAdapter(
                client,
                daily_quota_units=self.settings.youtube_daily_quota_units,
                min_poll_interval_seconds=self.settings.youtube_min_poll_interval_seconds,
                state_store=BotStateRepository(self.database),
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

        if self.settings.twitter_enabled:
            twitter_client = TwitterClient(timeout_seconds=self.settings.monitoring_http_timeout_seconds)
            self.monitoring.register_adapter(
                TwitterAdapter(twitter_client, min_poll_interval_seconds=self.settings.twitter_min_poll_interval_seconds)
            )
        else:
            logger.warning("TWITTER_ENABLED=false — X (Twitter) monitoring is disabled.")

        if self.settings.tiktok_enabled:
            tiktok_client = TikTokClient(timeout_seconds=self.settings.monitoring_http_timeout_seconds)
            self.monitoring.register_adapter(
                TikTokAdapter(tiktok_client, min_poll_interval_seconds=self.settings.tiktok_min_poll_interval_seconds)
            )
        else:
            logger.warning("TIKTOK_ENABLED=false — TikTok monitoring is disabled.")

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

    async def apply_presence(self) -> None:
        """The bot's status: "🛠️ Under maintenance" (Do Not Disturb) while
        maintenance is on, otherwise the configured DISCORD_STATUS_TEXT."""
        if self.maintenance.active:
            await self.change_presence(
                status=discord.Status.dnd, activity=discord.CustomActivity(name="🛠️ Under maintenance")
            )
            return
        text = self.settings.discord_status_text
        await self.change_presence(
            status=discord.Status.online, activity=discord.CustomActivity(name=text) if text else None
        )

    async def _monitorable_guilds(self, guild_ids: Set[int]) -> Set[int]:
        if self.maintenance.active:
            return set()  # maintenance pauses monitoring everywhere
        enabled = {guild_id for guild_id in guild_ids if await self.guilds_repo.is_enabled(guild_id)}
        return await self.billing.allowed_guild_ids(enabled)

    async def _billing_loop(self) -> None:
        last_reminder_check: Optional[datetime.datetime] = None
        last_dm_purge: Optional[datetime.datetime] = None
        while True:
            try:
                if await self.maintenance.refresh():  # the admin panel's switch flipped
                    await self.apply_presence()
            except Exception:
                logger.exception("Syncing maintenance mode failed.")
            try:
                await self.billing.poll_once()
            except Exception:
                logger.exception("Billing poll failed.")
            try:
                await self.mail.process_outbox()
            except Exception:
                logger.exception("Sending queued emails failed.")
            now = datetime.datetime.now(datetime.timezone.utc)
            if not self.maintenance.active and (
                last_reminder_check is None or now - last_reminder_check >= _REMINDER_CHECK_EVERY
            ):
                last_reminder_check = now
                names = {guild.id: guild.name for guild in self.guilds}
                try:
                    await self.mail.send_lapse_reminders(self.billing, names)
                except Exception:
                    logger.exception("Queueing lapse reminders failed.")
                try:
                    await self.mail.send_renewal_reminders(self.billing, names)
                except Exception:
                    logger.exception("Queueing payment-coming-up reminders failed.")
                try:
                    await self.send_activation_followups()
                except Exception:
                    logger.exception("Sending activation follow-ups failed.")
                if last_dm_purge is None or now - last_dm_purge >= datetime.timedelta(days=1):
                    last_dm_purge = now
                    try:
                        await self.dm_log.purge_older_than(_DM_LOG_RETENTION_DAYS)
                    except Exception:
                        logger.warning("Couldn't purge old DM log rows.", exc_info=True)
            await asyncio.sleep(self.settings.billing_poll_interval_seconds)

    async def send_activation_dm(self, guild: discord.Guild, user_id: int, *, followup: bool = False) -> bool:
        """The DM that tells whoever added the bot to activate the free trial
        on the website — once on joining, and once more a day later if it
        still hasn't been done. Nothing else is ever DMed to members."""
        url = self.billing.activate_url
        if followup:
            lines = (
                f"Your free trial for **{guild.name}** hasn't started yet — alerts won't run until you activate it.",
                "It takes a minute: log in with Discord, pick your server, done. The trial begins the moment you do.",
            )
            title = "🎁 Your free trial is waiting"
        else:
            lines = (
                f"Thanks for adding DiscoNotify to **{guild.name}**!",
                "To start your free trial, activate it on our website — log in with Discord, pick your server, "
                "and the trial begins right away.",
            )
            title = "🎁 Activate your free trial"
        button = discord.ui.Button(style=discord.ButtonStyle.link, label="Activate free trial", url=url, emoji="🎁")
        view = cards.card(*lines, title=title, tone=cards.INFO, extra=[discord.ui.ActionRow(button)])
        return await self._dm_user(
            user_id, view, guild_id=guild.id, kind="activation_reminder" if followup else "activation"
        )

    async def send_activation_followups(self) -> None:
        """One follow-up DM, a day after the first, for new servers that still
        haven't activated. Only to the person who added the bot, only if the
        first DM was delivered, and never again."""
        now = datetime.datetime.now(datetime.timezone.utc)
        for row in await self.billing.awaiting_activation():
            invited_by = row.get("invited_by")
            created = parse_time(row.get("created_at"))
            guild = self.get_guild(int(row["guild_id"]))
            if not invited_by or created is None or guild is None:
                continue
            if not _ACTIVATION_FOLLOWUP_AFTER <= now - created <= _ACTIVATION_FOLLOWUP_WINDOW:
                continue
            try:
                if await self.dm_log.status_of(guild.id, "activation") != "sent":
                    continue
                if await self.dm_log.status_of(guild.id, "activation_reminder") is not None:
                    continue
            except Exception:
                logger.warning("Couldn't read the DM log; skipping activation follow-ups.", exc_info=True)
                return
            await self.send_activation_dm(guild, int(invited_by), followup=True)

    async def _dm_user(
        self, user_id: int, view: discord.ui.LayoutView, *, guild_id: int, kind: str, payment_id: Optional[str] = None
    ) -> bool:
        """DMs a member and records the attempt in the DM log (IDs, kind and
        outcome only). Logging never gets in the way of sending."""
        status, error = "sent", None
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(view=view)
        except discord.HTTPException as exc:
            if exc.code == _DM_CLOSED:
                status = "dm_closed"
            else:
                status, error = "failed", f"HTTP {exc.status}, Discord code {exc.code}"
            logger.info("Couldn't DM %s about server %s (%s).", user_id, guild_id, status)
        try:
            await self.dm_log.add(
                guild_id=guild_id, user_id=user_id, kind=kind, status=status, error=error, payment_id=payment_id
            )
        except Exception:
            logger.warning("Couldn't record the %s DM to %s in the DM log (migration 0011?).", kind, user_id, exc_info=True)
        return status == "sent"

    async def _post_in_guild(self, guild: discord.Guild, view: discord.ui.LayoutView) -> bool:
        from bot.onboarding import pick_intro_channel

        channel = pick_intro_channel(guild)
        if channel is None:
            return False
        try:
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            return False

    async def _announce_payment(self, payment: Dict[str, Any], access: Access) -> None:
        """A payment or card subscription began: the customer gets an email
        (never a DM), a payment made from a Discord channel is also confirmed
        in that channel, and the bot owners are told."""
        guild_id = int(payment["guild_id"])
        row = await self.billing.billing_row(guild_id) or {}
        guild = self.get_guild(guild_id)
        try:
            await self.mail.on_payment(payment, access, row, guild.name if guild is not None else f"server {guild_id}")
        except Exception:
            logger.warning("Couldn't queue the subscription email for server %s.", guild_id, exc_info=True)

        channel_id = payment.get("channel_id")
        try:
            if channel_id:
                thanks = f"Thanks <@{payment['requested_by']}>! " if payment.get("requested_by") else "Thank you! "
                where = f" in **{guild.name}**" if guild is not None else ""
                if payment["provider"] == "polar" and row.get("card_status") == "trialing":
                    # Started during the free trial: card saved, nothing charged yet.
                    first_charge = f"<t:{int(access.until.timestamp())}:D>" if access.until else "the end of the trial"
                    view = cards.card(
                        f"{thanks}DiscoNotify's card subscription{where} is set up — the first "
                        f"{self.billing.price_label} charge is on {first_charge}, when the free trial ends.",
                        "Cancel before then with `/notify cancel` and you pay nothing.",
                        title="✅ Subscription set up",
                        tone=cards.SUCCESS,
                    )
                else:
                    until = f" — active until <t:{int(access.until.timestamp())}:D>" if access.until else ""
                    method = "card" if payment["provider"] == "polar" else "crypto"
                    view = cards.card(
                        f"{thanks}DiscoNotify's subscription{where} is active{until}.",
                        f"Paid by {method} · {payment['months']} month(s)",
                        title="✅ Payment received",
                        tone=cards.SUCCESS,
                    )
                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())  # type: ignore[union-attr]
        finally:
            method = "card" if payment["provider"] == "polar" else "crypto"
            await self._alert_bot_owners(
                "💰 New payment", f"{method} · {payment['months']} month(s) · ${payment['amount_usd']}", guild_id, payment, cards.SUCCESS
            )

    async def _announce_billing_event(self, event: BillingEvent) -> None:
        """What happened to a subscription after it began: the customer is
        emailed where it matters (a failed card payment, a cancellation) and
        the bot owners are told about every event. Never a DM to members."""
        guild = self.get_guild(event.guild_id)
        name = guild.name if guild is not None else f"server {event.guild_id}"
        try:
            await self.mail.on_billing_event(event, name)
        except Exception:
            logger.warning("Couldn't queue the %s email for server %s.", event.kind, event.guild_id, exc_info=True)
        summaries = {
            "card_renewed": (f"card renewed · {self.billing.price_label}", cards.SUCCESS),
            "card_failed": ("card charge failed (retrying)", cards.WARNING),
            "card_cancel_scheduled": ("card cancelled, ends at period end", cards.WARNING),
            "card_ended": ("card subscription ended", cards.ERROR),
            "crypto_failed": ("crypto payment failed/underpaid", cards.ERROR),
            "crypto_expired": ("crypto payment expired unpaid", cards.INFO),
        }
        summary, tone = summaries.get(event.kind, (event.kind, cards.INFO))
        await self._alert_bot_owners(f"Billing: {event.kind.replace('_', ' ')}", summary, event.guild_id, event.payment, tone)

    async def _alert_bot_owners(
        self, title: str, summary: str, guild_id: int, payment: Optional[Dict[str, Any]], tone: int
    ) -> None:
        if not self.settings.billing_owner_alerts:
            return
        guild = self.get_guild(guild_id)
        lines = [f"**{guild.name if guild else 'Unknown server'}** (`{guild_id}`)", summary]
        if payment and payment.get("requested_by"):
            lines.append(f"Requested by <@{payment['requested_by']}>")
        view = cards.card(*lines, title=title, tone=tone)
        for owner_id in self.settings.discord_owner_ids:
            try:
                user = self.get_user(owner_id) or await self.fetch_user(owner_id)
                await user.send(view=view)
            except discord.HTTPException:
                logger.info("Couldn't DM bot owner %s a billing alert.", owner_id)

    async def close(self) -> None:
        logger.info("Shutting down DiscoNotify...")
        if self._billing_task is not None:
            self._billing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._billing_task
        await self.billing.aclose()
        await self.mail.aclose()
        await self.monitoring.stop()
        await self.notification_sender.aclose()
        await self.database.close()
        await super().close()
