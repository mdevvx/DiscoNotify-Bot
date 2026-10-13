"""Application-wide constants that don't vary by environment.

Kept framework-agnostic (plain ints/strings) so this module has no
dependency on discord.py — callers convert e.g. a color int to
discord.Color where they actually build an embed.
"""

from __future__ import annotations

BOT_NAME = "PulseNotify"
BOT_VERSION = "0.1.0"

# Brand colour, sampled from the app icon (2026-10-13) — the default accent
# for every card the bot sends.
COLOR_PRIMARY = 0xE2364A

COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_INFO = COLOR_PRIMARY
COLOR_WARNING = 0xF1C40F

DEFAULT_COMMAND_PREFIX = "!"
