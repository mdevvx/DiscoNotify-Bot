-- PulseNotify: 0011_dm_log
--
-- One row per DM the bot attempts to a server member (purchaser or owner),
-- sent or not, so the admin panel can show per-member delivery status.
-- IDs, kind and status only: no message text, no emails.
--
-- Run after 0010_bot_present.sql.

create table if not exists pulsenotify.pulsenotify_dm_log (
    id               bigint generated always as identity primary key,
    guild_id         bigint not null,
    discord_user_id  bigint not null,
    kind             text not null,   -- checkout_abandoned | payment_success | payment_failed | card_cancelling | card_ended | expiry_reminder
    status           text not null check (status in ('sent', 'failed', 'dm_closed')),
    error            text,            -- short reason only, no stack traces
    payment_id       uuid references pulsenotify.pulsenotify_payments (id) on delete set null,
    created_at       timestamptz not null default now()
);

create index if not exists idx_pulsenotify_dm_log_guild on pulsenotify.pulsenotify_dm_log (guild_id, created_at desc);
create index if not exists idx_pulsenotify_dm_log_user on pulsenotify.pulsenotify_dm_log (discord_user_id, created_at desc);
create index if not exists idx_pulsenotify_dm_log_payment_kind on pulsenotify.pulsenotify_dm_log (payment_id, kind) where payment_id is not null;

-- Service role only; no public policies.
alter table pulsenotify.pulsenotify_dm_log enable row level security;
grant all on pulsenotify.pulsenotify_dm_log to service_role;
