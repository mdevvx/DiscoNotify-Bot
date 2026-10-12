"""Process-wide configuration, loaded once from the environment (and .env locally).

Only Discord and Supabase credentials are required to boot the bot. Every
platform API key is optional here — each platform adapter validates what it
specifically needs when that adapter is added, so a missing key disables
just that platform instead of blocking startup (see the startup flow in the
project spec: a non-critical platform being unavailable must not stop the
bot from starting).
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings tries to JSON-decode env values for any "complex" field
# type (list/dict/set) before pydantic's own validators ever run, so a
# comma-separated string like "111,222" fails as invalid JSON. Owner IDs are
# therefore stored as a raw string field and exposed as a list via a plain
# property below, which sidesteps that JSON-decoding step entirely.

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_dir: str = Field(default="logs", alias="LOG_DIR")

    discord_token: str = Field(..., min_length=1, alias="DISCORD_TOKEN")
    discord_command_prefix: str = Field(default="pn!", alias="DISCORD_COMMAND_PREFIX")
    discord_owner_ids_raw: str = Field(default="", alias="DISCORD_OWNER_IDS")
    discord_dev_guild_id: Optional[int] = Field(default=None, alias="DEV_GUILD_ID")

    supabase_url: str = Field(..., min_length=1, alias="SUPABASE_URL")
    supabase_key: str = Field(..., min_length=1, alias="SUPABASE_KEY")

    # Platform credentials are intentionally optional in the foundation phase.
    # X API v2 (Phase 7) — a static app-only Bearer token from the X
    # Developer Portal, not a key/secret pair (see platforms/twitter/adapter.py
    # for why, and the real per-read cost: X has no free read tier as of
    # 2026).
    twitter_bearer_token: Optional[str] = Field(default=None, alias="TWITTER_BEARER_TOKEN")
    youtube_api_key: Optional[str] = Field(default=None, alias="YOUTUBE_API_KEY")
    twitch_client_id: Optional[str] = Field(default=None, alias="TWITCH_CLIENT_ID")
    twitch_client_secret: Optional[str] = Field(default=None, alias="TWITCH_CLIENT_SECRET")
    # Kick's Public API (docs.kick.com) authenticates the same way Twitch's
    # does — an OAuth client-credentials grant, not a single opaque API
    # key — so this is a client_id/secret pair, not *_API_KEY.
    kick_client_id: Optional[str] = Field(default=None, alias="KICK_CLIENT_ID")
    kick_client_secret: Optional[str] = Field(default=None, alias="KICK_CLIENT_SECRET")
    # Instagram Graph API Business Discovery (Phase 7) — a long-lived Page
    # access token belonging to the *bot operator's own* connected
    # Instagram professional account, plus that account's own IG user ID
    # (see platforms/instagram/adapter.py for exactly why both are needed
    # and what this can/can't monitor). Renewed manually every ~60 days;
    # no in-app OAuth refresh flow is implemented.
    instagram_access_token: Optional[str] = Field(default=None, alias="INSTAGRAM_ACCESS_TOKEN")
    instagram_business_account_id: Optional[str] = Field(default=None, alias="INSTAGRAM_BUSINESS_ACCOUNT_ID")
    # Facebook and TikTok have no adapter (Phase 7) — neither platform
    # offers an official, ToS-compliant way to monitor a page/creator that
    # hasn't individually authorized this app. See README's Phase 7
    # section for the research behind that conclusion.

    # Monitoring framework configuration (section 28: one place for these,
    # not scattered through the code). Content polling is far less
    # time-sensitive than live status, hence the separate interval.
    monitoring_content_poll_interval_seconds: float = Field(default=300.0, alias="MONITORING_CONTENT_POLL_INTERVAL_SECONDS")
    monitoring_live_poll_interval_seconds: float = Field(default=60.0, alias="MONITORING_LIVE_POLL_INTERVAL_SECONDS")
    monitoring_max_retry_attempts: int = Field(default=5, alias="MONITORING_MAX_RETRY_ATTEMPTS")
    monitoring_retry_base_delay_seconds: float = Field(default=2.0, alias="MONITORING_RETRY_BASE_DELAY_SECONDS")
    monitoring_retry_max_delay_seconds: float = Field(default=300.0, alias="MONITORING_RETRY_MAX_DELAY_SECONDS")
    monitoring_rate_limit_requests: int = Field(default=30, alias="MONITORING_RATE_LIMIT_REQUESTS")
    monitoring_rate_limit_period_seconds: float = Field(default=60.0, alias="MONITORING_RATE_LIMIT_PERIOD_SECONDS")
    monitoring_event_queue_size: int = Field(default=1000, alias="MONITORING_EVENT_QUEUE_SIZE")
    # Consecutive platform-level failures before platform_health flips to
    # 'degraded', then 'error'. Per-account failure thresholds live on the
    # account itself (platform_accounts.consecutive_failures / .status) and
    # aren't configured here — they're about one broken account, not the
    # platform's API as a whole.
    monitoring_health_degraded_after: int = Field(default=3, alias="MONITORING_HEALTH_DEGRADED_AFTER")
    monitoring_health_error_after: int = Field(default=8, alias="MONITORING_HEALTH_ERROR_AFTER")
    monitoring_http_timeout_seconds: float = Field(default=15.0, alias="MONITORING_HTTP_TIMEOUT_SECONDS")

    # YouTube Data API v3 quota (see platforms/youtube/adapter.py for the
    # full breakdown): free-tier default is 10,000 units/day, shared across
    # every YouTube account this bot monitors, in every guild — it's one
    # budget per API key, not per account. Each channel check costs at most
    # 2 units; the adapter stretches the poll interval with the channel
    # count so the day's checks fit, but never polls faster than
    # youtube_min_poll_interval_seconds.
    youtube_daily_quota_units: int = Field(default=10_000, alias="YOUTUBE_DAILY_QUOTA_UNITS")
    youtube_min_poll_interval_seconds: float = Field(default=30.0, alias="YOUTUBE_MIN_POLL_INTERVAL_SECONDS")

    @property
    def discord_owner_ids(self) -> List[int]:
        return [int(part) for part in self.discord_owner_ids_raw.split(",") if part.strip()]

    @field_validator("discord_dev_guild_id", mode="before")
    @classmethod
    def _blank_dev_guild_id_means_unset(cls, value: object) -> object:
        # .env.example documents leaving this blank to mean "no dev guild" —
        # pydantic won't coerce "" to None for an Optional[int] on its own,
        # so a real .env with DEV_GUILD_ID= (unset) would otherwise fail
        # Settings validation and stop the bot from starting at all.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}")
        return upper


@lru_cache
def get_settings() -> Settings:
    """Returns the process-wide Settings singleton, loading it on first use."""
    return Settings()  # type: ignore[call-arg]  # populated from the environment, not kwargs
