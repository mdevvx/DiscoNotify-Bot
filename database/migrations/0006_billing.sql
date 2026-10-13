-- PulseNotify: 0006_billing
--
-- Per-server subscriptions: a 7-day trial, then $1/month by card (Paddle,
-- recurring) or crypto (NOWPayments, prepaid in 3/6/12-month blocks), plus
-- a bot-owner whitelist that never needs to pay. See billing/ for the
-- logic; nothing here is enforced until BILLING_ENABLED=true.
--
-- Run after 0005_move_to_pulsenotify_schema.sql (tables live in the
-- pulsenotify schema).

-- ── pulsenotify.pulsenotify_billing ──────────────────────────────────
-- One row per server. Access is granted while ANY of these holds:
--   whitelisted, now < trial_ends_at, now < paid_until + grace.
-- paid_until is extended by a card subscription's billing period or by a
-- crypto prepayment, whichever reaches further.
create table if not exists pulsenotify.pulsenotify_billing (
    guild_id                 bigint primary key references pulsenotify.pulsenotify_guilds (guild_id) on delete cascade,
    trial_ends_at            timestamptz not null,
    paid_until               timestamptz,
    whitelisted              boolean not null default false,
    whitelist_note           text,
    whitelisted_by           bigint,
    whitelisted_at           timestamptz,
    -- Paddle (card) recurring subscription, once one exists.
    card_subscription_id     text unique,
    card_customer_id         text,
    card_status              text,            -- Paddle's own: active, past_due, paused, canceled, trialing
    card_cancels_at          timestamptz,     -- set when the customer cancelled; access runs until then
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now()
);

drop trigger if exists trg_pulsenotify_billing_updated_at on pulsenotify.pulsenotify_billing;
create trigger trg_pulsenotify_billing_updated_at
    before update on pulsenotify.pulsenotify_billing
    for each row
    execute function pulsenotify.pulsenotify_set_updated_at();

alter table pulsenotify.pulsenotify_billing enable row level security;

-- ── pulsenotify.pulsenotify_payments ─────────────────────────────────
-- Every checkout the bot started: a Paddle transaction (card) or a
-- NOWPayments payment (crypto). The poller works through `pending` rows;
-- external_id is unique so a payment can only ever be credited once.
create table if not exists pulsenotify.pulsenotify_payments (
    id              uuid primary key default gen_random_uuid(),
    guild_id        bigint not null references pulsenotify.pulsenotify_guilds (guild_id) on delete cascade,
    provider        text not null check (provider in ('paddle', 'nowpayments')),
    external_id     text not null,
    months          integer not null check (months > 0),
    amount_usd      numeric(10, 2) not null,
    status          text not null default 'pending' check (status in ('pending', 'paid', 'failed', 'expired')),
    requested_by    bigint not null,
    channel_id      bigint,               -- where to post the "payment received" confirmation
    checkout_url    text,
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    paid_at         timestamptz,

    constraint uq_pulsenotify_payments_provider_external unique (provider, external_id)
);

create index if not exists idx_pulsenotify_payments_pending
    on pulsenotify.pulsenotify_payments (status, created_at) where status = 'pending';
create index if not exists idx_pulsenotify_payments_guild on pulsenotify.pulsenotify_payments (guild_id, created_at desc);

alter table pulsenotify.pulsenotify_payments enable row level security;

grant all on pulsenotify.pulsenotify_billing to service_role;
grant all on pulsenotify.pulsenotify_payments to service_role;

-- No rows are seeded here: a server's billing row (and its 7-day trial) is
-- created the first time the bot checks it with BILLING_ENABLED=true, so
-- running this migration early never starts anyone's trial clock.
