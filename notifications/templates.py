"""Safe placeholder substitution for admin-authored custom notification
messages (section 35's "Custom message... safe template substitution").

Two different trust levels are handled deliberately:

- The *template* itself is admin-authored (typed via
  `/notify account-set-message`, which requires Manage Server) and is
  allowed to contain literal Discord mention syntax on purpose — an admin
  writing "Hey @everyone" into their own template is explicit
  configuration, not the "arbitrary user-controlled content from social
  platforms" section 36 warns against.
- The *values* substituted into it (video title, creator name, ...) come
  from external platform content and are mention-escaped before
  insertion, so a stream title containing "@everyone" can never inject a
  mention the admin didn't type themselves.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

import discord

from events.models import NormalizedEvent

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Keep in sync with the description shown by /notify account-set-message
# and /notify help.
PLACEHOLDERS = ("creator", "title", "description", "url", "platform", "category", "viewers", "mention")

# Leaves headroom under Discord's 2000-char message limit after
# placeholder substitution can only grow the text.
MAX_TEMPLATE_LENGTH = 1500
_MAX_RENDERED_LENGTH = 2000


def format_role_mention(role_id: int, guild_id: Optional[int]) -> str:
    """Discord's @everyone role shares its ID with the guild, and `<@&id>`
    for it renders as a dead "@@everyone" that pings nobody — it has to be
    sent as the literal text instead."""
    if role_id == guild_id:
        return "@everyone"
    return f"<@&{role_id}>"


def render_custom_message(template: str, event: NormalizedEvent, *, mention_role_id: Optional[int] = None) -> str:
    values = _template_values(event, mention_role_id)

    def _replace(match: "re.Match[str]") -> str:
        # An unrecognized {placeholder} is left as literal text rather
        # than raising — a malformed template must never crash delivery.
        return values.get(match.group(1), match.group(0))

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    if len(rendered) > _MAX_RENDERED_LENGTH:
        rendered = rendered[: _MAX_RENDERED_LENGTH - 1].rstrip() + "…"
    return rendered


def _template_values(event: NormalizedEvent, mention_role_id: Optional[int]) -> Dict[str, str]:
    return {
        "creator": discord.utils.escape_mentions(event.account_username or ""),
        "title": discord.utils.escape_mentions(event.title or ""),
        "description": discord.utils.escape_mentions(event.description or ""),
        "url": event.url or "",  # a URL can't carry mention syntax; left unescaped
        "platform": event.platform.title(),
        "category": discord.utils.escape_mentions(str(event.metadata.get("category") or "")),
        "viewers": str(event.metadata.get("viewer_count") or ""),
        "mention": format_role_mention(mention_role_id, event.guild_id) if mention_role_id else "",
    }
