"""The PulseNotify Discord client.

Owns the services every command/cog needs (database, repositories,
monitoring, notification delivery) and the startup/shutdown lifecycle.
Registering a new platform adapter is the only thing _register_platform_adapters()
needs to grow for — everything else here is already platform-agnostic.
"""

from __future__ import annotations

import datetime
from typing import Optional, cast

import discord
from discord import app_commands
from discord.ext import commands

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
)


class PulseNotifyCommandTree(app_commands.CommandTree):
    """Slash command tree that gates every command behind the guild's enabled flag.

    `/pulsenotify toggle` itself is always allowed through, since a
    disabled server must still be able to run the one command that
    re-enables it.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True

        command = interaction.command
        if command is not None and command.qualified_name == "pulsenotify toggle":
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
            self.monitoring.register_adapter(KickAdapter(kick_client))
        else:
            logger.warning("KICK_CLIENT_ID/KICK_CLIENT_SECRET not set — Kick monitoring is disabled.")

        if self.settings.instagram_access_token and self.settings.instagram_business_account_id:
            instagram_client = InstagramClient(
                self.settings.instagram_access_token,
                self.settings.instagram_business_account_id,
                timeout_seconds=self.settings.monitoring_http_timeout_seconds,
            )
            self.monitoring.register_adapter(InstagramAdapter(instagram_client))
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

    async def close(self) -> None:
        logger.info("Shutting down PulseNotify...")
        await self.monitoring.stop()
        await self.database.close()
        await super().close()
