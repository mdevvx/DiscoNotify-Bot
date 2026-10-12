"""Application-wide constants that don't vary by environment.

Kept framework-agnostic (plain ints/strings) so this module has no
dependency on discord.py — callers convert e.g. a color int to
discord.Color where they actually build an embed.
"""

from __future__ import annotations

BOT_NAME = "PulseNotify"
BOT_VERSION = "0.1.0"

COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_INFO = 0x5865F2
COLOR_WARNING = 0xF1C40F

DEFAULT_COMMAND_PREFIX = "!"
