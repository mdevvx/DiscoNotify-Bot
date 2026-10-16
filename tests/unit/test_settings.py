from __future__ import annotations

import pytest

from config.settings import Settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")


def test_settings_parses_owner_ids_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DISCORD_OWNER_IDS", "111, 222,333")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.discord_owner_ids == [111, 222, 333]


def test_settings_defaults_owner_ids_to_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("DISCORD_OWNER_IDS", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.discord_owner_ids == []


def test_settings_rejects_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")

    with pytest.raises(Exception):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_requires_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-key")

    with pytest.raises(Exception):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_platform_keys_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for var in ("TWITTER_ENABLED", "YOUTUBE_API_KEY", "TWITCH_CLIENT_ID", "KICK_CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.twitter_enabled is True  # X needs no key
    assert settings.youtube_api_key is None


def test_blank_dev_guild_id_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example documents leaving DEV_GUILD_ID blank to mean "no dev
    guild" — a real .env with DEV_GUILD_ID= (the literal example value)
    must not crash Settings()."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DEV_GUILD_ID", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.discord_dev_guild_id is None


def test_dev_guild_id_with_a_real_value_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DEV_GUILD_ID", "123456789")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.discord_dev_guild_id == 123456789


def test_blank_required_fields_raise_a_clear_error_not_a_silent_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required field present-but-blank (as opposed to entirely unset)
    must fail loudly at startup, not silently pass through as "" and fail
    confusingly later when something tries to use it (e.g. an empty
    Supabase URL)."""
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_KEY", "")

    with pytest.raises(Exception):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_a_bad_variable_is_reported_by_name_without_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real case 2026-10-16: BILLING_POLL_INTERVAL_SECONDS=600e crash-looped a
    deploy behind pages of traceback."""
    from pydantic import ValidationError

    from config.settings import describe_settings_errors

    _set_required_env(monkeypatch)
    monkeypatch.setenv("BILLING_POLL_INTERVAL_SECONDS", "600e")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    text = describe_settings_errors(exc_info.value)
    assert "BILLING_POLL_INTERVAL_SECONDS" in text and "valid number" in text
    assert "600e" not in text


def test_a_missing_required_variable_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from config.settings import describe_settings_errors

    _set_required_env(monkeypatch)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "SUPABASE_KEY: Field required" in describe_settings_errors(exc_info.value)
