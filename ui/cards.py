"""Components V2 message cards for every command reply.

Every reply the bot sends — confirmations, errors, lists, status — is a
`discord.ui.LayoutView` holding one accent-coloured `Container`, never
plain message content or a legacy embed. One helper keeps them consistent
(title, body, optional fields, small footer) and makes "never ping from a
reply" the default rather than something each command has to remember.

Notifications (alerts for new videos, streams, posts) have their own
layouts in notifications/cards.py, built on the same components.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

import discord

from config import constants

SUCCESS = constants.COLOR_SUCCESS
ERROR = constants.COLOR_ERROR
INFO = constants.COLOR_INFO
WARNING = constants.COLOR_WARNING


def card(
    *blocks: str,
    title: Optional[str] = None,
    tone: int = INFO,
    fields: Sequence[Tuple[str, str]] = (),
    footer: Optional[str] = None,
    extra: Iterable[discord.ui.Item] = (),
) -> discord.ui.LayoutView:
    """A one-container card: `### title`, each block as its own text, each
    (name, value) field as a bold heading over its value, then any `extra`
    components (e.g. an ActionRow of buttons), then a small footer."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container(*blocks, title=title, tone=tone, fields=fields, footer=footer, extra=extra))
    return view


def container(
    *blocks: str,
    title: Optional[str] = None,
    tone: int = INFO,
    fields: Sequence[Tuple[str, str]] = (),
    footer: Optional[str] = None,
    extra: Iterable[discord.ui.Item] = (),
) -> discord.ui.Container:
    children: list = []
    if title:
        children.append(discord.ui.TextDisplay(f"### {title}"))
    children.extend(discord.ui.TextDisplay(block) for block in blocks if block)
    if fields:
        if children:
            children.append(discord.ui.Separator())
        children.extend(discord.ui.TextDisplay(f"**{name}**\n{value}") for name, value in fields)
    children.extend(extra)
    if footer:
        children.append(discord.ui.TextDisplay(f"-# {footer}"))
    return discord.ui.Container(*children, accent_colour=tone)


async def reply(
    interaction: discord.Interaction,
    *blocks: str,
    ephemeral: bool = False,
    allowed_mentions: Optional[discord.AllowedMentions] = None,
    view: Optional[discord.ui.LayoutView] = None,
    **card_options,
) -> None:
    """Sends a card as the interaction's response, or as a followup if the
    interaction was already responded to (e.g. deferred)."""
    layout = view if view is not None else card(*blocks, **card_options)
    mentions = allowed_mentions if allowed_mentions is not None else discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(view=layout, ephemeral=ephemeral, allowed_mentions=mentions)
    else:
        await interaction.response.send_message(view=layout, ephemeral=ephemeral, allowed_mentions=mentions)


async def error(interaction: discord.Interaction, message: str) -> None:
    """A private red card — for anything the user needs to fix."""
    await reply(interaction, message, tone=ERROR, ephemeral=True)


def plain_text(view: Optional[discord.ui.LayoutView]) -> str:
    """Every text block in a layout, joined by newlines — for logs and
    tests, which care about what a card says, not how it's nested."""
    if view is None:
        return ""
    parts = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            parts.append(item.content)
    return "\n".join(parts)
