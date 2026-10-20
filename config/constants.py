"""Application-wide constants that don't vary by environment.

Kept framework-agnostic (plain ints/strings) so this module has no
dependency on discord.py — callers convert e.g. a color int to
discord.Color where they actually build an embed.
"""

from __future__ import annotations

BOT_NAME = "DiscoNotify"
BOT_VERSION = "0.1.0"

# Card colours, from the app icon (2026-10-14): everything normal —
# replies, confirmations, public and private cards — is the logo's white;
# anything that failed or needs attention is the logo's red. (Alert cards
# keep their platform's colour: notifications/cards.py.)
COLOR_LOGO_WHITE = 0xFFFFFF
COLOR_LOGO_RED = 0xE2364A

COLOR_PRIMARY = COLOR_LOGO_WHITE
COLOR_INFO = COLOR_LOGO_WHITE
COLOR_SUCCESS = COLOR_LOGO_WHITE
COLOR_ERROR = COLOR_LOGO_RED
COLOR_WARNING = COLOR_LOGO_RED

DEFAULT_COMMAND_PREFIX = "!"
