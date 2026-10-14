-- PulseNotify: 0007_bot_state
--
-- Bot-wide settings that must survive a restart, as key → JSON value.
-- First user: maintenance mode (`!maintenance on`), stored here so the bot
-- stays in maintenance across the restart it was switched on for.
-- Run after 0006_billing.sql.

create table if not exists pulsenotify.pulsenotify_bot_state (
    key         text primary key,
    value       jsonb not null default '{}'::jsonb,
    updated_at  timestamptz not null default now()
);

drop trigger if exists trg_pulsenotify_bot_state_updated_at on pulsenotify.pulsenotify_bot_state;
create trigger trg_pulsenotify_bot_state_updated_at
    before update on pulsenotify.pulsenotify_bot_state
    for each row
    execute function pulsenotify.pulsenotify_set_updated_at();

alter table pulsenotify.pulsenotify_bot_state enable row level security;

grant all on pulsenotify.pulsenotify_bot_state to service_role;
