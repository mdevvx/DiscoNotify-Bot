"""`/notify account-add|account-remove|account-list|account-set-message|
account-set-mention|account-info|alerts` — account registration (Phase 4)
plus per-account alert configuration (Phase 4's account-set-message, and
Phase 8's account-set-mention/alerts/account-info).

Flattened as direct subcommands of the shared notify_group rather
than nested under their own "account" subcommand group: Discord doesn't
allow mixing plain subcommands and subcommand groups as siblings under
the same parent, and `/notify toggle`/`status` are plain subcommands
— so these are too, for consistency and to avoid that conflict.

Platform-agnostic by design (section 52) — the `platform` choice list is
the only thing that grows as later phases add adapters; everything else
(validation, duplicate prevention, channel linking, default alert config)
already works for any PlatformAdapter.

Commands that take more than a pick-one choice open a modal form for the
details (commands/accounts/modals.py); the slash command itself only
selects the platform or the account.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from commands.accounts.modals import (
    EVENT_TYPE_LABELS,
    PLATFORM_FORMS,
    AddAccountModal,
    AlertsModal,
    CustomMessageModal,
    MentionModal,
)
from commands.notify_group import notify_group
from notifications.templates import format_role_mention
from ui import cards
from utils.logger import get_logger

logger = get_logger(__name__)

# Only platforms that are finalized are offered (PLATFORM_FORMS). Twitch has
# an adapter under platforms/ but is held back until it's finalized;
# re-adding it is an entry in PLATFORM_FORMS plus its registration in
# bot/client.py's _register_platform_adapters().
_PLATFORM_CHOICES = [app_commands.Choice(name=form.name, value=value) for value, form in PLATFORM_FORMS.items()]

_STATUS_ICONS = {"active": "🟢", "invalid": "🔴", "error": "🟡"}

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


async def _resolve_account(interaction: discord.Interaction, account: str) -> Optional[Dict[str, Any]]:
    """The guild's account row for an autocomplete value, or None after
    telling the user why not."""
    assert interaction.guild is not None
    if not _is_valid_account_id(account):
        await cards.error(interaction, _NOT_A_VALID_ACCOUNT_MESSAGE)
        return None
    row = await interaction.client.accounts_repo.get_by_guild(interaction.guild.id, account)  # type: ignore[attr-defined]
    if row is None:
        await cards.error(interaction, "That account isn't configured in this server.")
    return row


async def _resolve_account_configs(
    interaction: discord.Interaction, account: str
) -> "Optional[tuple[Dict[str, Any], List[Dict[str, Any]]]]":
    row = await _resolve_account(interaction, account)
    if row is None:
        return None
    configs = await interaction.client.alert_configs_repo.list_for_account(account)  # type: ignore[attr-defined]
    if not configs:
        await cards.error(interaction, f"**{row['username']}** has no alert configuration yet.")
        return None
    return row, configs


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


@notify_group.command(
    name="account-add", description="Start monitoring a platform account and post alerts to a channel."
)
@app_commands.describe(platform="Which platform to monitor — a form opens for the details")
@app_commands.choices(platform=_PLATFORM_CHOICES)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_add(interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
    await interaction.response.send_modal(AddAccountModal(platform.value))


@notify_group.command(name="account-remove", description="Stop monitoring an account.")
@app_commands.describe(account="The account to stop monitoring")
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_remove(interaction: discord.Interaction, account: str) -> None:
    assert interaction.guild is not None
    row = await _resolve_account(interaction, account)
    if row is None:
        return

    view = RemoveAccountConfirmView(owner_id=interaction.user.id, guild_id=interaction.guild.id, row=row)
    await cards.reply(interaction, view=view, ephemeral=True)
    view.prompt = interaction


# How long the remove confirmation buttons stay usable.
_CONFIRM_TIMEOUT_SECONDS = 60.0


class RemoveAccountConfirmView(discord.ui.LayoutView):
    """The account-remove prompt: a warning card with Remove/Cancel buttons
    inside it. Only the admin who ran the command can press them; nothing
    is deleted unless Remove is pressed before the timeout."""

    def __init__(self, *, owner_id: int, guild_id: int, row: Dict[str, Any]) -> None:
        super().__init__(timeout=_CONFIRM_TIMEOUT_SECONDS)
        self._owner_id = owner_id
        self._guild_id = guild_id
        self._row = row
        # The interaction that sent the prompt — used to edit it on timeout.
        self.prompt: Optional[discord.Interaction] = None

        remove_button: discord.ui.Button = discord.ui.Button(label="Remove", style=discord.ButtonStyle.danger)
        cancel_button: discord.ui.Button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        remove_button.callback = self.confirm  # type: ignore[method-assign]
        cancel_button.callback = self.cancel  # type: ignore[method-assign]
        self.add_item(
            cards.container(
                f"Stop monitoring **{row['username']}** ({row['platform'].title()})?",
                "This also deletes its alert settings and notification history. This can't be undone.",
                title="Remove account?",
                tone=cards.WARNING,
                extra=[discord.ui.ActionRow(remove_button, cancel_button)],
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await cards.error(interaction, "Only the person who ran this command can confirm it.")
            return False
        return True

    async def confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        row = self._row
        removed = await interaction.client.accounts_repo.remove(self._guild_id, row["id"])  # type: ignore[attr-defined]
        if not removed:
            # Someone else removed it while this prompt was open.
            await interaction.response.edit_message(view=cards.card(f"**{row['username']}** was already removed."))
            return

        logger.info(
            "Guild %s removed %s account %s (%s)", self._guild_id, row["platform"], row["username"], row["id"]
        )
        await interaction.response.edit_message(view=cards.card("Removed.", tone=cards.SUCCESS))
        # Public, so other admins can see who stopped monitoring what.
        await cards.reply(
            interaction,
            f"{interaction.user.mention} stopped monitoring **{row['username']}** ({row['platform'].title()}).",
            title="Account removed",
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=cards.card(f"Cancelled — still monitoring **{self._row['username']}**.")
        )

    async def on_timeout(self) -> None:
        if self.prompt is None:
            return
        try:
            await self.prompt.edit_original_response(
                view=cards.card(f"Timed out — still monitoring **{self._row['username']}**.")
            )
        except discord.HTTPException:
            pass  # the prompt was dismissed; nothing to update


@notify_group.command(name="account-list", description="List accounts being monitored in this server.")
async def account_list(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    bot = interaction.client
    accounts = await bot.accounts_repo.list_by_guild(interaction.guild.id)  # type: ignore[attr-defined]
    if not accounts:
        await cards.reply(
            interaction,
            "No accounts are being monitored in this server yet. Use `/notify account-add` to add one.",
            title="Monitored Accounts",
            ephemeral=True,
        )
        return

    lines = []
    for row in accounts:
        icon = _STATUS_ICONS.get(row["status"], "⚪")
        suffix = "" if row["enabled"] else " *(disabled)*"
        lines.append(f"{icon} **{row['username']}** — {row['platform'].title()}{suffix}")

    await cards.reply(interaction, "\n".join(lines), title="Monitored Accounts", footer=f"{len(accounts)} account(s)")


@notify_group.command(
    name="account-set-message", description="Set a custom notification message for an account (opens a form)."
)
@app_commands.describe(account="The account to customize")
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_set_message(interaction: discord.Interaction, account: str) -> None:
    resolved = await _resolve_account_configs(interaction, account)
    if resolved is not None:
        await interaction.response.send_modal(CustomMessageModal(*resolved))


@notify_group.command(
    name="account-set-mention", description="Choose who an account's alerts mention (opens a form)."
)
@app_commands.describe(account="The account to configure")
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def account_set_mention(interaction: discord.Interaction, account: str) -> None:
    assert interaction.guild is not None
    resolved = await _resolve_account_configs(interaction, account)
    if resolved is not None:
        await interaction.response.send_modal(MentionModal(*resolved, guild_id=interaction.guild.id))


@notify_group.command(
    name="alerts", description="Turn an account's alert types and embeds on or off (opens a form)."
)
@app_commands.describe(account="The account to configure")
@app_commands.autocomplete(account=_account_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def alerts(interaction: discord.Interaction, account: str) -> None:
    resolved = await _resolve_account_configs(interaction, account)
    if resolved is not None:
        await interaction.response.send_modal(AlertsModal(*resolved))


@notify_group.command(
    name="account-info", description="Show an account's full alert configuration — per event type."
)
@app_commands.describe(account="The account to inspect")
@app_commands.autocomplete(account=_account_autocomplete)
async def account_info(interaction: discord.Interaction, account: str) -> None:
    assert interaction.guild is not None
    resolved = await _resolve_account_configs(interaction, account)
    if resolved is None:
        return
    row, configs = resolved

    fields = []
    for config in sorted(configs, key=lambda c: c["event_type"]):
        label = EVENT_TYPE_LABELS.get(config["event_type"], config["event_type"])
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
        fields.append((label, value))

    # cards.reply never pings, so a custom message containing @everyone
    # (shown above) is displayed, not triggered.
    await cards.reply(interaction, title=f"{row['username']} — {row['platform'].title()}", fields=fields)


async def setup(bot: commands.Bot) -> None:
    """No Cog needed — these attach to the shared notify_group at
    import time (the decorators above)."""
