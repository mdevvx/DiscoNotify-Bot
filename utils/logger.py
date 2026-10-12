"""Process-wide logging setup.

Loggers are obtained via get_logger(__name__) throughout the codebase, so
output is naturally namespaced by module (bot.client, database.client,
commands.admin.toggle, ...) without needing separate handlers per
subsystem. A rotating file handler keeps a bounded on-disk history so a
long-running deployment doesn't fill its volume.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _SuppressVoiceWarning(logging.Filter):
    """Drops discord.py's "voice will NOT be supported" startup notice.

    PulseNotify has no voice features at all (it only ever posts text
    alerts), so the missing voice-encryption dependency this warns about
    is never relevant — silenced by message content rather than by
    lowering discord.client's whole log level, so any other warning that
    logger emits still comes through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configures the root logger once for the whole process. Safe to call more than once."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    log_path = Path(log_dir)
    if not log_path.is_absolute():
        # Relative to the project, not to wherever the bot was started from
        # — otherwise the log quietly lands in a different folder per launch.
        log_path = Path(__file__).resolve().parent.parent / log_path
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path / "pulsenotify.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # These are noisy at our default levels and rarely useful for our own debugging.
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("discord.client").addFilter(_SuppressVoiceWarning())

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
