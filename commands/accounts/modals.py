"""Modal forms behind the account commands (`account-add`,
`account-set-message`, `account-set-mention`, `alerts`).

A slash command picks *what* to act on (the platform, or an account via
autocomplete — a select menu inside a modal is capped at 25 options, which
a busy server can exceed), then opens one of these forms for the details:
text fields, channel/role pickers and multi-selects, pre-filled with the
current settings.

Each modal's on_submit only reads its components and hands plain values to
apply(), which holds the actual logic — so tests drive apply() directly
without faking Discord's component state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import discord

from events.models import EventType, NormalizedEvent
from notifications.templates import MAX_TEMPLATE_LENGTH, PLACEHOLDERS, format_role_mention, render_custom_message
from platforms.base import event_types_for_capabilities
from ui import cards
from utils.errors import PermanentPlatformError
from utils.logger import get_logger

logger = get_logger(__name__)

# A form left open this long is abandoned; Discord rejects a late submit.
_MODAL_TIMEOUT_SECONDS = 600.0

EVENT_TYPE_LABELS: Dict[str, str] = {
    EventType.VIDEO_PUBLISHED.value: "New Video",
    EventType.SHORT_PUBLISHED.value: "New Short",
    EventType.POST_CREATED.value: "New Post",
    EventType.STREAM_STARTED.value: "Live Started",
    EventType.STREAM_ENDED.value: "Live Ended",
    EventType.STREAM_UPDATED.value: "Stream Updated",
    EventType.ACCOUNT_UPDATED.value: "Account Updated",
}

_ALL_EVENT_TYPES = "__all__"


@dataclass(frozen=True)
class PlatformForm:
    name: str
    account_label: str
    placeholder: str


PLATFORM_FORMS: Dict[str, PlatformForm] = {
    "youtube": PlatformForm("YouTube", "YouTube channel", "@handle, channel URL, or channel ID"),
    "kick": PlatformForm("Kick", "Kick channel", "Channel name or kick.com URL"),
    "instagram": PlatformForm(
        "Instagram", "Instagram account", "Username or instagram.com URL (Business/Creator only)"
    ),
}


def _label(event_type: str) -> str:
    return EVENT_TYPE_LABELS.get(event_type, event_type)


def _sorted_types(configs: Sequence[Dict[str, Any]]) -> List[str]:
    return sorted({config["event_type"] for config in configs}, key=_label)


def _scope_options(configs: Sequence[Dict[str, Any]]) -> List[discord.SelectOption]:
    options = [discord.SelectOption(label="All alert types", value=_ALL_EVENT_TYPES, default=True)]
    options += [discord.SelectOption(label=_label(t), value=t) for t in _sorted_types(configs)]
    return options


def _scope_text(event_type: Optional[str]) -> str:
    return f"for **{_label(event_type)}**" if event_type else "for all its alert types"


def _shared_value(configs: Sequence[Dict[str, Any]], key: str) -> Any:
    """The value every event type agrees on, else None — what a form
    pre-fills when it applies to all event types at once."""
    values = {config.get(key) for config in configs}
    return values.pop() if len(values) == 1 else None


# ── account-add ─────────────────────────────────────────────────────────


class AddAccountModal(discord.ui.Modal):
    def __init__(self, platform: str) -> None:
        form = PLATFORM_FORMS[platform]
        super().__init__(title=f"Add a {form.name} account", timeout=_MODAL_TIMEOUT_SECONDS)
        self.platform = platform
        self.identifier = discord.ui.TextInput(placeholder=form.placeholder, max_length=200)
        self.channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Pick a channel",
            required=True,
        )
        self.role = discord.ui.RoleSelect(placeholder="No mention", min_values=0, required=False)
        self.add_item(discord.ui.Label(text=form.account_label, component=self.identifier))
        self.add_item(discord.ui.Label(text="Post alerts in", component=self.channel))
        self.add_item(
            discord.ui.Label(
                text="Role to mention (optional)",
                description="Pinged on every alert. Change later with /pulsenotify account-set-mention.",
                component=self.role,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        picked = self.channel.values[0] if self.channel.values else None
        channel = interaction.guild.get_channel(picked.id) if picked is not None else None
        role_id = self.role.values[0].id if self.role.values else None
        await self.apply(interaction, identifier=self.identifier.value, channel=channel, role_id=role_id)

    async def apply(
        self, interaction: discord.Interaction, *, identifier: str, channel: Any, role_id: Optional[int]
    ) -> None:
        assert interaction.guild is not None
        bot = interaction.client
        form = PLATFORM_FORMS[self.platform]
        identifier = identifier.strip()
        if not identifier:
            await cards.error(interaction, f"Enter a {form.account_label}.")
            return
        if channel is None:
            await cards.error(interaction, "I can't see that channel — pick another one.")
            return
        me = interaction.guild.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not (permissions.view_channel and permissions.send_messages):
                await cards.error(
                    interaction,
                    f"I can't post in {channel.mention} — give me **View Channel** and **Send Messages** there, "
                    "or pick another channel.",
                )
                return

        await interaction.response.defer(thinking=True)

        adapter = bot.monitoring.get_adapter(self.platform)  # type: ignore[attr-defined]
        if adapter is None:
            await cards.error(interaction, f"{form.name} isn't available right now (no API credentials configured for it).")
            return

        try:
            account_ref = await adapter.validate_account(identifier)
        except PermanentPlatformError as exc:
            await cards.error(interaction, f"Couldn't find that {form.name} account: {exc}")
            return
        except Exception:
            logger.exception("Error validating %s account %r for guild %s", self.platform, identifier, interaction.guild.id)
            await cards.error(interaction, f"Couldn't reach {form.name} right now — try again in a moment.")
            return

        account_row = await bot.accounts_repo.add(  # type: ignore[attr-defined]
            interaction.guild.id, self.platform, account_ref.platform_account_id, account_ref.username
        )
        if account_row is None:
            await cards.error(interaction, f"**{account_ref.username}** is already being monitored in this server.")
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
        mention_text = ""
        if role_id is not None:
            await bot.alert_configs_repo.set_mention_role(account_row["id"], role_id)  # type: ignore[attr-defined]
            mention_text = f", mentioning {format_role_mention(role_id, interaction.guild.id)}"

        logger.info(
            "Guild %s added %s account %s (%s), alerts -> #%s",
            interaction.guild.id,
            self.platform,
            account_ref.username,
            account_row["id"],
            channel.name,
        )
        await cards.reply(
            interaction,
            f"Now monitoring **{account_ref.username}** on {form.name} — alerts will post in "
            f"{channel.mention}{mention_text}.",
            title="Account added",
            tone=cards.SUCCESS,
        )


# ── account-set-message ─────────────────────────────────────────────────


class CustomMessageModal(discord.ui.Modal):
    def __init__(self, row: Dict[str, Any], configs: Sequence[Dict[str, Any]]) -> None:
        super().__init__(title=f"Alert message — {row['username']}"[:45], timeout=_MODAL_TIMEOUT_SECONDS)
        self.row = row
        self.message = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="{creator} just posted: {title}\n{url}",
            default=_shared_value(configs, "custom_message"),
            required=False,
            max_length=MAX_TEMPLATE_LENGTH,
        )
        self.scope = discord.ui.Select(options=_scope_options(configs))
        self.add_item(
            discord.ui.Label(
                text="Message",
                description=("Placeholders: " + " ".join(f"{{{p}}}" for p in PLACEHOLDERS) + ". Empty = default.")[:100],
                component=self.message,
            )
        )
        self.add_item(discord.ui.Label(text="Applies to", component=self.scope))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        event_type = self.scope.values[0] if self.scope.values else _ALL_EVENT_TYPES
        await self.apply(interaction, message=self.message.value, event_type=event_type)

    async def apply(self, interaction: discord.Interaction, *, message: str, event_type: str) -> None:
        assert interaction.guild is not None
        row = self.row
        scoped_type = None if event_type == _ALL_EVENT_TYPES else event_type
        clearing = not message.strip()

        updated = await interaction.client.alert_configs_repo.set_custom_message(  # type: ignore[attr-defined]
            row["id"], None if clearing else message, event_type=scoped_type
        )
        if updated == 0:
            await cards.error(interaction, f"**{row['username']}** doesn't have that alert type configured.")
            return

        scope = _scope_text(scoped_type)
        logger.info(
            "Guild %s %s custom message for account %s (%s) %s",
            interaction.guild.id,
            "cleared" if clearing else "set",
            row["username"],
            row["id"],
            scope,
        )
        if clearing:
            await cards.reply(
                interaction, f"**{row['username']}** uses the default message again {scope}.", title="Message reset"
            )
            return

        preview = render_custom_message(
            message,
            NormalizedEvent(
                platform=row["platform"],
                platform_account_id=row["platform_account_id"],
                account_username=row["username"],
                event_type=EventType(scoped_type) if scoped_type else EventType.VIDEO_PUBLISHED,
                platform_event_id="preview",
                title="Example Title",
                url="https://example.com/preview-link",
            ),
        )
        # cards.reply never pings — the preview can contain a literal
        # @everyone typed into the template.
        await cards.reply(
            interaction,
            f"Custom message set for **{row['username']}** {scope}.",
            title="Message updated",
            tone=cards.SUCCESS,
            fields=[("Preview", preview)],
        )


# ── account-set-mention ─────────────────────────────────────────────────

_MENTION_ROLE = "role"
_MENTION_EVERYONE = "everyone"
_MENTION_NONE = "none"


class MentionModal(discord.ui.Modal):
    """The @everyone role isn't offered by Discord's role picker, so who to
    ping is its own choice, with the role picker used for "a role"."""

    def __init__(self, row: Dict[str, Any], configs: Sequence[Dict[str, Any]], guild_id: int) -> None:
        super().__init__(title=f"Mention — {row['username']}"[:45], timeout=_MODAL_TIMEOUT_SECONDS)
        self.row = row
        current = _shared_value(configs, "mention_role_id")
        current_mode = _MENTION_NONE if current is None else _MENTION_EVERYONE if current == guild_id else _MENTION_ROLE
        self.mode = discord.ui.Select(
            options=[
                discord.SelectOption(label="A role (pick below)", value=_MENTION_ROLE, default=current_mode == _MENTION_ROLE),
                discord.SelectOption(label="@everyone", value=_MENTION_EVERYONE, default=current_mode == _MENTION_EVERYONE),
                discord.SelectOption(label="Nobody", value=_MENTION_NONE, default=current_mode == _MENTION_NONE),
            ]
        )
        self.role = discord.ui.RoleSelect(
            placeholder="Pick a role",
            min_values=0,
            required=False,
            default_values=[discord.Object(id=current)] if current_mode == _MENTION_ROLE else [],
        )
        self.scope = discord.ui.Select(options=_scope_options(configs))
        self.add_item(discord.ui.Label(text="Who to mention", component=self.mode))
        self.add_item(discord.ui.Label(text="Role", description="Only used for \"A role\".", component=self.role))
        self.add_item(discord.ui.Label(text="Applies to", component=self.scope))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.apply(
            interaction,
            mode=self.mode.values[0] if self.mode.values else _MENTION_NONE,
            role_id=self.role.values[0].id if self.role.values else None,
            event_type=self.scope.values[0] if self.scope.values else _ALL_EVENT_TYPES,
        )

    async def apply(
        self, interaction: discord.Interaction, *, mode: str, role_id: Optional[int], event_type: str
    ) -> None:
        assert interaction.guild is not None
        row = self.row
        if mode == _MENTION_ROLE and role_id is None:
            await cards.error(interaction, "Pick a role, or choose @everyone or Nobody.")
            return
        if mode == _MENTION_EVERYONE:
            role_id = interaction.guild.id  # the @everyone role's ID is the server's ID
        elif mode == _MENTION_NONE:
            role_id = None

        scoped_type = None if event_type == _ALL_EVENT_TYPES else event_type
        updated = await interaction.client.alert_configs_repo.set_mention_role(  # type: ignore[attr-defined]
            row["id"], role_id, event_type=scoped_type
        )
        if updated == 0:
            await cards.error(interaction, f"**{row['username']}** doesn't have that alert type configured.")
            return

        scope = _scope_text(scoped_type)
        logger.info(
            "Guild %s %s mention role for account %s (%s) %s",
            interaction.guild.id,
            "cleared" if role_id is None else f"set to role {role_id}",
            row["username"],
            row["id"],
            scope,
        )
        if role_id is None:
            await cards.reply(interaction, f"**{row['username']}** won't mention anyone {scope}.", title="Mention cleared")
            return
        # format_role_mention, not Role.mention: the latter renders the
        # @everyone role as a dead "@@everyone". cards.reply never pings.
        await cards.reply(
            interaction,
            f"**{row['username']}** will now mention {format_role_mention(role_id, interaction.guild.id)} {scope}.",
            title="Mention updated",
            tone=cards.SUCCESS,
        )


# ── alerts ──────────────────────────────────────────────────────────────


class AlertsModal(discord.ui.Modal):
    def __init__(self, row: Dict[str, Any], configs: Sequence[Dict[str, Any]]) -> None:
        super().__init__(title=f"Alerts — {row['username']}"[:45], timeout=_MODAL_TIMEOUT_SECONDS)
        self.row = row
        self.event_types = _sorted_types(configs)
        by_type = {config["event_type"]: config for config in configs}

        def options(key: str) -> List[discord.SelectOption]:
            return [
                discord.SelectOption(label=_label(t), value=t, default=bool(by_type[t][key])) for t in self.event_types
            ]

        count = len(self.event_types)
        self.enabled = discord.ui.Select(
            options=options("enabled"), min_values=0, max_values=count, required=False, placeholder="No alerts"
        )
        self.embeds = discord.ui.Select(
            options=options("embed_enabled"), min_values=0, max_values=count, required=False, placeholder="None"
        )
        self.add_item(discord.ui.Label(text="Send alerts for", component=self.enabled))
        self.add_item(
            discord.ui.Label(
                text="Show embed / link preview for",
                description="Unselected types are sent as plain text with no preview.",
                component=self.embeds,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.apply(interaction, enabled=list(self.enabled.values), embeds=list(self.embeds.values))

    async def apply(self, interaction: discord.Interaction, *, enabled: Sequence[str], embeds: Sequence[str]) -> None:
        assert interaction.guild is not None
        row = self.row
        lines = []
        for event_type in self.event_types:
            is_enabled = event_type in enabled
            has_embed = event_type in embeds
            await interaction.client.alert_configs_repo.set_alert_toggle(  # type: ignore[attr-defined]
                row["id"], event_type, enabled=is_enabled, embed_enabled=has_embed
            )
            state = "✅ On" if is_enabled else "❌ Off"
            embed_state = "embed on" if has_embed else "embed off"
            lines.append(f"**{_label(event_type)}** — {state} · {embed_state}")

        logger.info(
            "Guild %s updated alerts for account %s (%s): enabled=%s embeds=%s",
            interaction.guild.id,
            row["username"],
            row["id"],
            sorted(enabled),
            sorted(embeds),
        )
        await cards.reply(
            interaction, "\n".join(lines), title=f"Alerts updated — {row['username']}", tone=cards.SUCCESS
        )
