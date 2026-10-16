-- PulseNotify: 0010_bot_present
--
-- Whether the bot is in each server right now. Settings are kept when the bot
-- is removed (re-adding restores them), so a row alone doesn't mean the bot is
-- there — the website's "pick your server" list (docs/website-integration.md)
-- only offers servers where bot_present is true. The bot keeps it up to date
-- (joined / removed / startup sync).
--
-- Run after 0009_polar_provider.sql.

alter table pulsenotify.pulsenotify_guilds
    add column if not exists bot_present boolean not null default true;
