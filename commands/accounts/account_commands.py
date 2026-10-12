"""`/pulsenotify account-add|account-remove|account-list|account-set-message|
account-set-mention|account-info|alerts` — account registration (Phase 4)
plus per-account alert configuration (Phase 4's account-set-message, and
Phase 8's account-set-mention/alerts/account-info).

Flattened as direct subcommands of the shared pulsenotify_group rather
than nested under their own "account" subcommand group: Discord doesn't
allow mixing plain subcommands and subcommand groups as siblings under
the same parent, and `/pulsenotify toggle`/`status` are plain subcommands
— so these are too, for consistency and to avoid that conflict.

Platform-agnostic by design (section 52) — the `platform` choice list is
the only thing that grows as later phases add adapters; everything else
(validation, duplicate prevention, channel linking, default alert config)
already works for any PlatformAdapter.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from commands.pulsenotify_group import pulsenotify_group
from config import constants
from events.models import EventType, NormalizedEvent
from notifications.templates import MAX_TEMPLATE_LENGTH, PLACEHOLDERS, format_role_mention, render_custom_message
from platforms.base import event_types_for_capabilities
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

# Only platforms that are finalized (or actively being finalized) are
# offered. Twitch and X (Twitter) have adapters under platforms/ but are
# held back until they're finalized; re-adding one is a Choice here plus
# its registration in bot/client.py's _register_platform_adapters().
_PLATFORM_CHOICES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="Kick", value="kick"),
    app_commands.Choice(name="Instagram", value="instagram"),
]

_STATUS_ICONS = {"active": "🟢", "invalid": "🔴", "error": "🟡"}

_EVENT_TYPE_CHOICES = [
    app_commands.Choice(name="New Video", value=EventType.VIDEO_PUBLISHED.value),
    app_commands.Choice(name="New Short", value=EventType.SHORT_PUBLISHED.value),
    app_commands.Choice(name="New Post", value=EventType.POST_CREATED.value),
    app_commands.Choice(name="Live Started", value=EventType.STREAM_STARTED.value),
    app_commands.Choice(name="Live Ended", value=EventType.STREAM_ENDED.value),
    app_commands.Choice(name="Stream Updated", value=EventType.STREAM_UPDATED.value),
    app_commands.Choice(name="Account Updated", value=EventType.ACCOUNT_UPDATED.value),
]

_EVENT_TYPE_LABELS = {choice.value: choice.name for choice in _EVENT_TYPE_CHOICES}

_CLEAR_KEYWORDS = {"clear", "reset", "default"}

_NOT_A_VALID_ACCOUNT_MESSAGE = (
    "That's not a valid account selection. Start typing and pick one of the suggestions Discord "
    "shows you — typing a name and submitting it directly (without picking a suggestion) doesn't work."
)


def _is_valid_account_id(value: str) -> bool:
    # account_id is a database uuid; the autocomplete below submits
    # row["id"] as the Choice *value*, but Discord's autocomplete is only
    # a suggestion for a plain string parameter — nothing stops someone
    # from typing free text (e.g. the suggestion's display label) and
    # submitting that instead. Catching it here turns what would
    # otherwise be a raw Postgres "invalid input syntax for type uuid"
    # error into a clear, actionable message.
    try:
        uuid_lib.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def _account_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    accounts = await interaction.client.accounts_repo.list_by_guild(interaction.guild.id)  # type: ignore[attr-defined]
    current_lower = current.lower()
    matches = [
        row
        for row in accounts
        if current_lower in row["username"].lower() or current_lower in row["platform"].lower()
    ]
    return [
        app_commands.Choice(name=f"{row['platform'].title()}: {row['username']}", value=row["id"])
        for row in matches[:25]  # Discord caps autocomplete results at 25
    ]


@pulsenotify_group.command(
    name="account-add", description="Start monitoring a platform account and post alerts to a channel."
)
@app_commands.describe(
    platform="Which platform to monitor",
    identifier="Channel ID, @handle, or channel URL",
    channel="Discord channel to post alerts in",
)
@app_commands.choices(platform=_PLATFORM_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_add(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
    identifier: str,
    channel: discord.TextChannel,
) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    identifier = identifier.strip()
    if not identifier:
        await interaction.response.send_message("Please provide a channel ID, handle, or URL.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    adapter = bot.monitoring.get_adapter(platform.value)  # type: ignore[attr-defined]
    if adapter is None:
        await interaction.followup.send(
            f"{platform.name} isn't available right now (no API credentials configured for it)."
        )
        return

    try:
        account_ref = await adapter.validate_account(identifier)
    except PermanentPlatformError as exc:
        await interaction.followup.send(f"Couldn't find that {platform.name} account: {exc}")
        return
    except Exception:
        logger.exception("Error validating %s account %r for guild %s", platform.value, identifier, interaction.guild.id)
        await interaction.followup.send(f"Couldn't reach {platform.name} right now — try again in a moment.")
        return

    account_row = await bot.accounts_repo.add(  # type: ignore[attr-defined]
        interaction.guild.id, platform.value, account_ref.platform_account_id, account_ref.username
    )
    if account_row is None:
        await interaction.followup.send(f"**{account_ref.username}** is already being monitored in this server.")
        return

    channel_row = await bot.channels_repo.get_or_create(  # type: ignore[attr-defined]
        interaction.guild.id, channel.id, name=channel.name
    )
    await bot.channels_repo.link_account(interaction.guild.id, account_row["id"], channel_row["id"])  # type: ignore[attr-defined]
    # Created here rather than waiting for the first notification, so the
    # admin can rename it / give it a custom avatar in Discord's own
    # channel-integrations UI before any alert ever goes out through it.
    await bot.notification_sender.get_or_create_webhook(channel_row, channel)  # type: ignore[attr-defined]

    event_types = event_types_for_capabilities(adapter.capabilities)
    await bot.alert_configs_repo.seed_defaults(  # type: ignore[attr-defined]
        interaction.guild.id, account_row["id"], [t.value for t in event_types]
    )

    logger.info(
        "Guild %s added %s account %s (%s), alerts -> #%s",
        interaction.guild.id,
        platform.value,
        account_ref.username,
        account_row["id"],
        channel.name,
    )
    await interaction.followup.send(
        f"✅ Now monitoring **{account_ref.username}** on {platform.name} — alerts will post in {channel.mention}."
    )


@pulsenotify_group.command(name="account-remove", description="Stop monitoring an account.")
@app_commands.describe(account="The account to stop monitoring")
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_remove(interaction: discord.Interaction, account: str) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    if not _is_valid_account_id(account):
        await interaction.response.send_message(_NOT_A_VALID_ACCOUNT_MESSAGE, ephemeral=True)
        return

    row = await bot.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await interaction.response.send_message("That account isn't configured in this server.", ephemeral=True)
        return

    await bot.accounts_repo.remove(interaction.guild.id, account)  # type: ignore[attr-defined]
    logger.info(
        "Guild %s removed %s account %s (%s)", interaction.guild.id, row["platform"], row["username"], account
    )
    await interaction.response.send_message(f"🗑️ Stopped monitoring **{row['username']}** ({row['platform'].title()}).")


@pulsenotify_group.command(name="account-list", description="List accounts being monitored in this server.")
async def account_list(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    accounts = await bot.accounts_repo.list_by_guild(interaction.guild.id)  # type: ignore[attr-defined]
    if not accounts:
        await interaction.response.send_message(
            "No accounts are being monitored in this server yet. Use `/pulsenotify account-add` to add one.",
            ephemeral=True,
        )
        return

    lines = []
    for row in accounts:
        icon = _STATUS_ICONS.get(row["status"], "⚪")
        suffix = "" if row["enabled"] else " *(disabled)*"
        lines.append(f"{icon} **{row['username']}** — {row['platform'].title()}{suffix}")

    embed = discord.Embed(
        title="Monitored Accounts", description="\n".join(lines), color=discord.Color(constants.COLOR_INFO)
    )
    await interaction.response.send_message(embed=embed)


@pulsenotify_group.command(
    name="account-set-message",
    description="Set a custom notification message for an account (or 'clear' to use the default).",
)
@app_commands.describe(
    account="The account to customize",
    message=(
        "Your message. Placeholders: " + ", ".join(f"{{{p}}}" for p in PLACEHOLDERS) + ". Type 'clear' to remove."
    ),
    event_type="Only apply to this event type (optional — applies to every event type this account currently has configured)",
)
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.choices(event_type=_EVENT_TYPE_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_set_message(
    interaction: discord.Interaction,
    account: str,
    message: str,
    event_type: Optional[app_commands.Choice[str]] = None,
) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    if not _is_valid_account_id(account):
        await interaction.response.send_message(_NOT_A_VALID_ACCOUNT_MESSAGE, ephemeral=True)
        return

    row = await bot.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await interaction.response.send_message("That account isn't configured in this server.", ephemeral=True)
        return

    clearing = message.strip().lower() in _CLEAR_KEYWORDS
    if not clearing and len(message) > MAX_TEMPLATE_LENGTH:
        await interaction.response.send_message(
            f"That message is too long ({len(message)} characters, max {MAX_TEMPLATE_LENGTH}).", ephemeral=True
        )
        return

    updated = await bot.alert_configs_repo.set_custom_message(  # type: ignore[attr-defined]
        account,
        None if clearing else message,
        event_type=event_type.value if event_type else None,
    )
    if updated == 0:
        scope = f" for the **{event_type.name}** alert type" if event_type else ""
        await interaction.response.send_message(
            f"**{row['username']}** doesn't have an alert configuration{scope} yet.", ephemeral=True
        )
        return

    scope = f"for **{event_type.name}**" if event_type else "for all its currently configured alert types"
    logger.info(
        "Guild %s %s custom message for account %s (%s) %s",
        interaction.guild.id,
        "cleared" if clearing else "set",
        row["username"],
        account,
        scope,
    )

    if clearing:
        await interaction.response.send_message(f"🧹 Cleared the custom message for **{row['username']}** {scope}.")
        return

    preview = render_custom_message(
        message,
        NormalizedEvent(
            platform=row["platform"],
            platform_account_id=row["platform_account_id"],
            account_username=row["username"],
            event_type=EventType(event_type.value) if event_type else EventType.VIDEO_PUBLISHED,
            platform_event_id="preview",
            title="Example Title",
            url="https://example.com/preview-link",
        ),
    )
    # A confirmation must never ping — the preview can contain a literal
    # @everyone typed into the template.
    await interaction.response.send_message(
        f"✅ Custom message set for **{row['username']}** {scope}.\n\n**Preview:**\n{preview}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@pulsenotify_group.command(
    name="account-set-mention",
    description="Set the role to mention for an account's alerts (omit role to clear it).",
)
@app_commands.describe(
    account="The account to configure",
    role="Role to mention when this account posts or goes live (omit to clear the mention)",
    event_type="Only apply to this event type (optional — applies to every event type this account currently has configured)",
)
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.choices(event_type=_EVENT_TYPE_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_set_mention(
    interaction: discord.Interaction,
    account: str,
    role: Optional[discord.Role] = None,
    event_type: Optional[app_commands.Choice[str]] = None,
) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    if not _is_valid_account_id(account):
        await interaction.response.send_message(_NOT_A_VALID_ACCOUNT_MESSAGE, ephemeral=True)
        return

    row = await bot.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await interaction.response.send_message("That account isn't configured in this server.", ephemeral=True)
        return

    updated = await bot.alert_configs_repo.set_mention_role(  # type: ignore[attr-defined]
        account,
        role.id if role else None,
        event_type=event_type.value if event_type else None,
    )
    if updated == 0:
        scope = f" for the **{event_type.name}** alert type" if event_type else ""
        await interaction.response.send_message(
            f"**{row['username']}** doesn't have an alert configuration{scope} yet.", ephemeral=True
        )
        return

    scope = f"for **{event_type.name}**" if event_type else "for all its currently configured alert types"
    logger.info(
        "Guild %s %s mention role for account %s (%s) %s",
        interaction.guild.id,
        "cleared" if role is None else f"set to role {role.id}",
        row["username"],
        account,
        scope,
    )

    if role is None:
        await interaction.response.send_message(f"🧹 Cleared the mention role for **{row['username']}** {scope}.")
        return

    # role.mention is literal "@everyone" for the default role; the
    # confirmation itself must not ping it.
    await interaction.response.send_message(
        f"✅ **{row['username']}** will now mention {role.mention} {scope}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@pulsenotify_group.command(
    name="alerts", description="View or change whether an event type notifies, and whether it uses an embed."
)
@app_commands.describe(
    account="The account to configure",
    event_type="Which event type to view or change",
    enabled="Turn this alert on or off (omit both this and embed to just view the current setting)",
    embed="Show the rich embed for this alert, instead of plain text (omit to leave unchanged)",
)
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.choices(event_type=_EVENT_TYPE_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
async def alerts(
    interaction: discord.Interaction,
    account: str,
    event_type: app_commands.Choice[str],
    enabled: Optional[bool] = None,
    embed: Optional[bool] = None,
) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    if not _is_valid_account_id(account):
        await interaction.response.send_message(_NOT_A_VALID_ACCOUNT_MESSAGE, ephemeral=True)
        return

    row = await bot.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await interaction.response.send_message("That account isn't configured in this server.", ephemeral=True)
        return

    if enabled is None and embed is None:
        config = await bot.alert_configs_repo.get(account, event_type.value)  # type: ignore[attr-defined]
        if config is None:
            await interaction.response.send_message(
                f"**{row['username']}** doesn't have an alert configuration for **{event_type.name}** yet.",
                ephemeral=True,
            )
            return
        state = "✅ On" if config["enabled"] else "❌ Off"
        embed_state = "✅ On" if config["embed_enabled"] else "❌ Off"
        await interaction.response.send_message(
            f"**{row['username']}** — {event_type.name}\nAlert: {state}\nEmbed: {embed_state}"
        )
        return

    updated = await bot.alert_configs_repo.set_alert_toggle(  # type: ignore[attr-defined]
        account, event_type.value, enabled=enabled, embed_enabled=embed
    )
    if updated == 0:
        await interaction.response.send_message(
            f"**{row['username']}** doesn't have an alert configuration for **{event_type.name}** yet.",
            ephemeral=True,
        )
        return

    changes = []
    if enabled is not None:
        changes.append(f"alert {'enabled' if enabled else 'disabled'}")
    if embed is not None:
        changes.append(f"embed {'enabled' if embed else 'disabled'}")
    logger.info(
        "Guild %s updated alerts for account %s (%s) %s: %s",
        interaction.guild.id,
        row["username"],
        account,
        event_type.value,
        ", ".join(changes),
    )
    await interaction.response.send_message(f"✅ **{row['username']}** — {event_type.name}: {', '.join(changes)}.")


@pulsenotify_group.command(
    name="account-info", description="Show an account's full alert configuration — per event type."
)
@app_commands.describe(account="The account to inspect")
@app_commands.autocomplete(account=_account_autocomplete)
async def account_info(interaction: discord.Interaction, account: str) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    if not _is_valid_account_id(account):
        await interaction.response.send_message(_NOT_A_VALID_ACCOUNT_MESSAGE, ephemeral=True)
        return

    row = await bot.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await interaction.response.send_message("That account isn't configured in this server.", ephemeral=True)
        return

    configs = await bot.alert_configs_repo.list_for_account(account)  # type: ignore[attr-defined]
    if not configs:
        await interaction.response.send_message(
            f"**{row['username']}** has no alert configuration yet.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"{row['username']} — {row['platform'].title()}", color=discord.Color(constants.COLOR_INFO)
    )
    for config in sorted(configs, key=lambda c: c["event_type"]):
        label = _EVENT_TYPE_LABELS.get(config["event_type"], config["event_type"])
        state = "✅ On" if config["enabled"] else "❌ Off"
        embed_state = "embed on" if config["embed_enabled"] else "embed off"
        mention = (
            format_role_mention(config["mention_role_id"], interaction.guild.id)
            if config["mention_role_id"]
            else "no mention"
        )
        value = f"{state} · {embed_state} · {mention}"
        if config.get("custom_message"):
            preview = config["custom_message"]
            if len(preview) > 150:
                preview = preview[:149] + "…"
            value += f"\nCustom message: {preview}"
        embed.add_field(name=label, value=value, inline=False)

    await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — these attach to the shared pulsenotify_group at
    import time (the decorators above)."""
