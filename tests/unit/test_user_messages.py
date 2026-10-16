"""What members are told when something goes wrong: plain English, a
reference code for the unexpected, and never technical text."""

from __future__ import annotations

import logging

import pytest

from utils.errors import PermanentPlatformError, PlatformError, RateLimitedError, TransientPlatformError
from utils.user_messages import friendly_platform_error, minutes_text


def test_minutes_are_rounded_up_and_singular_when_one() -> None:
    assert minutes_text(1) == "about 1 minute"
    assert minutes_text(61) == "about 2 minutes"
    assert minutes_text(None) == "about 1 minute"


def test_a_rate_limit_says_when_to_try_again() -> None:
    text = friendly_platform_error(RateLimitedError("API rate limited: code 4", retry_after=600), platform="Kick", context="x")

    assert text == "Kick is busy right now. Please try again in about 10 minutes."


def test_a_message_written_for_members_is_used_as_is() -> None:
    error = PermanentPlatformError("Kick 404: slug", user_message="We couldn't find that Kick channel.")

    assert friendly_platform_error(error, platform="Kick", context="x") == "We couldn't find that Kick channel."


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PermanentPlatformError("YouTube API 404: channel 123"), "We couldn't find that YouTube account."),
        (TransientPlatformError("YouTube API error 503: quota"), "YouTube isn't responding right now."),
        (PlatformError("anything else from a platform"), "YouTube isn't responding right now."),
        (ValueError("a bug"), "Something went wrong on our side."),
    ],
)
def test_everything_else_is_generic_with_a_reference_and_logged(error: Exception, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        text = friendly_platform_error(error, platform="YouTube", context="Adding a YouTube account")

    assert expected in text and "(Reference `" in text
    reference = text.split("`")[1]
    assert reference in caplog.text  # the owner can find the detail in the log by this code
    for technical in ("404", "503", "quota", "API", "123", "a bug"):
        assert technical not in text
