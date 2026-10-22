"""Why an email wasn't sent."""

from __future__ import annotations


class MailError(Exception):
    """The email wasn't sent."""


class TransientMailError(MailError):
    """Worth trying again later (timeout, the mail server is busy or down, a
    login problem that can be fixed in the settings)."""


class PermanentMailError(MailError):
    """Won't work however often it's retried (the address doesn't exist, the
    message was refused)."""
