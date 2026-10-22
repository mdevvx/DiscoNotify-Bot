-- DiscoNotify: 0013_email_and_trial_activation
--
-- 1. Trial activation: a NEW server's free trial starts when someone activates
--    it on the website (which also collects an email address), not when the
--    bot joins. Servers that already exist keep their trial exactly as it is
--    and never need to activate.
-- 2. Email: contacts, editable templates, and an outbox that the bot (auto
--    emails) and the admin panel (manual emails) both write to. The bot is
--    the only thing that talks to the email provider.
--
-- Run after 0012_rename_to_disconotify.sql (schema `disconotify`, tables
-- `notify_*`). Deploy the website and admin panel changes with it.

-- ── Trial activation ─────────────────────────────────────────────────

alter table disconotify.notify_billing alter column trial_ends_at drop not null;

-- true = this server's trial has not started and must be activated first.
-- Existing rows stay false, so nobody who already has a trial is paused.
alter table disconotify.notify_billing add column if not exists activation_required boolean not null default false;
alter table disconotify.notify_billing add column if not exists trial_activated_at timestamptz;
alter table disconotify.notify_billing add column if not exists activated_by bigint;   -- Discord user who activated
alter table disconotify.notify_billing add column if not exists invited_by bigint;     -- Discord user who added the bot (audit log), if known

-- Servers that already have a trial keep it: record when it started.
update disconotify.notify_billing set trial_activated_at = created_at where trial_activated_at is null;

-- Servers the bot is already in but that have no billing row yet would
-- otherwise become "needs activation" the first time the bot looks at them.
-- Give them the trial they would have got before this change.
insert into disconotify.notify_billing (guild_id, trial_ends_at, trial_activated_at)
select g.guild_id, now() + interval '7 days', now()
from disconotify.notify_guilds g
where g.bot_present
  and not exists (select 1 from disconotify.notify_billing b where b.guild_id = g.guild_id);

-- ── Email contacts ───────────────────────────────────────────────────
-- Who to email for a server. Filled by the website (trial activation and
-- checkout). Several people can be contacts for one server.
create table if not exists disconotify.notify_email_contacts (
    id                          uuid primary key default gen_random_uuid(),
    guild_id                    bigint not null references disconotify.notify_guilds (guild_id) on delete cascade,
    email                       text not null check (email = lower(email)),
    discord_user_id             bigint,
    source                      text not null default 'activation' check (source in ('activation', 'checkout', 'manual')),
    -- Set when the person unsubscribes from reminder/marketing emails.
    -- Billing emails (receipts, failed payments, cancellations) still go out.
    marketing_unsubscribed_at   timestamptz,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now(),
    constraint uq_notify_email_contacts unique (guild_id, email)
);

create index if not exists idx_notify_email_contacts_guild on disconotify.notify_email_contacts (guild_id);
create index if not exists idx_notify_email_contacts_email on disconotify.notify_email_contacts (email);

drop trigger if exists trg_notify_email_contacts_updated_at on disconotify.notify_email_contacts;
create trigger trg_notify_email_contacts_updated_at
    before update on disconotify.notify_email_contacts
    for each row execute function disconotify.notify_set_updated_at();

-- ── Email templates (edited in the admin panel) ──────────────────────
create table if not exists disconotify.notify_email_templates (
    key          text primary key,                 -- what the bot looks up, e.g. 'payment_failed'
    name         text not null,                    -- shown in the admin panel
    description  text not null default '',         -- when it is sent
    category     text not null check (category in ('transactional', 'marketing')),
    subject      text not null,
    preheader    text not null default '',         -- the grey preview line in inboxes
    body_html    text not null,
    body_text    text not null,
    enabled      boolean not null default true,
    version      integer not null default 1,
    updated_by   text,                              -- admin email
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- Every save keeps the previous version, so the admin panel can restore it.
create table if not exists disconotify.notify_email_template_versions (
    id            bigint generated always as identity primary key,
    template_key  text not null references disconotify.notify_email_templates (key) on delete cascade,
    version       integer not null,
    subject       text not null,
    preheader     text not null default '',
    body_html     text not null,
    body_text     text not null,
    saved_by      text,
    saved_at      timestamptz not null default now()
);

create index if not exists idx_notify_email_template_versions_key
    on disconotify.notify_email_template_versions (template_key, version desc);

create or replace function disconotify.notify_email_template_snapshot()
returns trigger language plpgsql as $$
begin
    if new.subject is distinct from old.subject
       or new.preheader is distinct from old.preheader
       or new.body_html is distinct from old.body_html
       or new.body_text is distinct from old.body_text then
        insert into disconotify.notify_email_template_versions
            (template_key, version, subject, preheader, body_html, body_text, saved_by, saved_at)
        values (old.key, old.version, old.subject, old.preheader, old.body_html, old.body_text, old.updated_by, old.updated_at);
        new.version := old.version + 1;
    end if;
    new.updated_at := now();
    return new;
end $$;

drop trigger if exists trg_notify_email_templates_snapshot on disconotify.notify_email_templates;
create trigger trg_notify_email_templates_snapshot
    before update on disconotify.notify_email_templates
    for each row execute function disconotify.notify_email_template_snapshot();

-- ── Email outbox / log ───────────────────────────────────────────────
-- One row per email to one person. Auto emails are queued by the bot, the
-- website (trial activation) and the admin panel (manual sends); the bot
-- sends whatever is `queued` within about a minute and records the result.
create table if not exists disconotify.notify_email_outbox (
    id               uuid primary key default gen_random_uuid(),
    guild_id         bigint references disconotify.notify_guilds (guild_id) on delete set null,
    to_email         text not null,
    template_key     text,                           -- null for a fully custom manual email
    kind             text not null default 'auto' check (kind in ('auto', 'manual')),
    variables        jsonb not null default '{}'::jsonb,
    -- Manual emails may override the template or be fully custom:
    subject_override text,
    body_html        text,
    body_text        text,
    status           text not null default 'queued'
                     check (status in ('queued', 'sending', 'sent', 'failed', 'skipped')),
    status_detail    text,                           -- error, or why it was skipped (unsubscribed, template disabled, ...)
    subject_sent     text,                           -- the subject as it was actually sent
    provider_id      text,                           -- the email provider's message id
    attempts         integer not null default 0,
    send_after       timestamptz not null default now(),
    -- Makes an auto email idempotent: the same key is never queued twice.
    dedupe_key       text unique,
    created_by       text,                           -- admin email for manual sends
    created_at       timestamptz not null default now(),
    sent_at          timestamptz
);

create index if not exists idx_notify_email_outbox_queue on disconotify.notify_email_outbox (status, send_after)
    where status = 'queued';
create index if not exists idx_notify_email_outbox_guild on disconotify.notify_email_outbox (guild_id, created_at desc);

-- ── Activating a trial (called by the website) ───────────────────────
-- One atomic, repeatable step so the website and the bot can't disagree:
--   * a server that still needs activation gets its trial started now;
--   * a server that already has a trial keeps its dates untouched — only the
--     email is saved;
--   * the email is stored as a contact, and a "trial activated" email is queued.
-- The website must already have checked (via Discord login) that the person
-- manages this server and that the email is Discord-verified.
create or replace function disconotify.notify_activate_trial(
    p_guild_id bigint,
    p_user_id bigint,
    p_email text,
    p_trial_days integer default 7
) returns jsonb
language plpgsql
security definer
set search_path = disconotify, public
as $$
declare
    v_email   text := lower(trim(p_email));
    v_row     disconotify.notify_billing%rowtype;
    v_status  text;
begin
    if v_email is null or v_email = '' or position('@' in v_email) = 0 then
        raise exception 'invalid email';
    end if;
    if not exists (select 1 from disconotify.notify_guilds where guild_id = p_guild_id and bot_present) then
        raise exception 'the bot is not in this server';
    end if;

    insert into disconotify.notify_billing (guild_id, activation_required)
    values (p_guild_id, true)
    on conflict (guild_id) do nothing;

    select * into v_row from disconotify.notify_billing where guild_id = p_guild_id for update;

    if v_row.trial_ends_at is null then
        update disconotify.notify_billing
           set trial_ends_at = now() + make_interval(days => greatest(p_trial_days, 0)),
               trial_activated_at = now(),
               activated_by = p_user_id
         where guild_id = p_guild_id
        returning * into v_row;
        v_status := 'activated';
    else
        update disconotify.notify_billing
           set trial_activated_at = coalesce(trial_activated_at, now()),
               activated_by = coalesce(activated_by, p_user_id)
         where guild_id = p_guild_id
        returning * into v_row;
        v_status := 'email_saved';
    end if;

    insert into disconotify.notify_email_contacts (guild_id, email, discord_user_id, source)
    values (p_guild_id, v_email, p_user_id, 'activation')
    on conflict (guild_id, email) do update set discord_user_id = coalesce(excluded.discord_user_id, notify_email_contacts.discord_user_id);

    if v_status = 'activated' then
        insert into disconotify.notify_email_outbox (guild_id, to_email, template_key, kind, variables, dedupe_key)
        values (
            p_guild_id, v_email, 'trial_activated', 'auto',
            jsonb_build_object('trial_days', p_trial_days, 'trial_ends_at', v_row.trial_ends_at),
            'trial_activated:' || p_guild_id
        )
        on conflict (dedupe_key) do nothing;
    end if;

    return jsonb_build_object(
        'status', v_status,
        'trial_ends_at', v_row.trial_ends_at,
        'whitelisted', v_row.whitelisted
    );
end $$;

revoke all on function disconotify.notify_activate_trial(bigint, bigint, text, integer) from public;
grant execute on function disconotify.notify_activate_trial(bigint, bigint, text, integer) to service_role;

-- ── Default templates ────────────────────────────────────────────────
-- Plain, brand-neutral starting copy; edit in the admin panel. Variables:
--   {{server_name}} {{price}} {{trial_days}} {{trial_ends_at}} {{paid_until}}
--   {{next_charge_date}} {{status_line}} {{subscribe_url}} {{manage_url}}
--   {{support_email}} {{reminder_number}} {{reminder_total}} {{unsubscribe_url}}
-- {{...}} in the HTML body is HTML-escaped; use {{{...}}} for a trusted link.
insert into disconotify.notify_email_templates (key, name, description, category, subject, preheader, body_html, body_text) values
('trial_activated', 'Trial activated', 'Sent when someone activates a server''s free trial on the website.', 'transactional',
 'Your DiscoNotify free trial has started',
 'Your trial for {{server_name}} runs until {{trial_ends_at}}.',
 '<h1>Your free trial has started</h1><p>Thanks for activating DiscoNotify for <strong>{{server_name}}</strong>.</p><p>Your free trial runs until <strong>{{trial_ends_at}}</strong>. After that it is {{price}}/month — you can subscribe any time from <a href="{{{subscribe_url}}}">our website</a>.</p><p>Questions? Write to {{support_email}}.</p>',
 E'Your free trial has started\n\nThanks for activating DiscoNotify for {{server_name}}.\n\nYour free trial runs until {{trial_ends_at}}. After that it is {{price}}/month - subscribe any time: {{subscribe_url}}\n\nQuestions? Write to {{support_email}}.'),
('subscription_started', 'Subscription started', 'Sent when a payment or card subscription for a server begins.', 'transactional',
 'Your DiscoNotify subscription is active',
 '{{status_line}}',
 '<h1>Thank you!</h1><p>DiscoNotify is set up for <strong>{{server_name}}</strong>.</p><p>{{status_line}}</p><p>You can manage or cancel your subscription any time from <a href="{{{manage_url}}}">your billing page</a>.</p><p>Questions? Write to {{support_email}}.</p>',
 E'Thank you!\n\nDiscoNotify is set up for {{server_name}}.\n\n{{status_line}}\n\nManage or cancel any time: {{manage_url}}\n\nQuestions? Write to {{support_email}}.'),
('subscription_cancelled', 'Subscription cancelled', 'Sent when a card subscription is cancelled.', 'transactional',
 'Your DiscoNotify subscription was cancelled',
 'You will not be charged again.',
 '<h1>Your subscription was cancelled</h1><p>The DiscoNotify subscription for <strong>{{server_name}}</strong> was cancelled and you will not be charged again.</p><p>{{status_line}}</p><p>Changed your mind? You can subscribe again any time at <a href="{{{subscribe_url}}}">our website</a>.</p>',
 E'Your subscription was cancelled\n\nThe DiscoNotify subscription for {{server_name}} was cancelled and you will not be charged again.\n\n{{status_line}}\n\nChanged your mind? Subscribe again: {{subscribe_url}}'),
('payment_failed', 'Payment past due', 'Sent when a card payment fails (it is retried automatically).', 'transactional',
 'Your DiscoNotify payment did not go through',
 'Please update your card to keep alerts running.',
 '<h1>Your payment did not go through</h1><p>We could not charge your card for DiscoNotify on <strong>{{server_name}}</strong> ({{price}}/month). We will try again automatically, and alerts keep running meanwhile.</p><p>To use a different card, open <a href="{{{manage_url}}}">your billing page</a>.</p><p>Need help? Write to {{support_email}}.</p>',
 E'Your payment did not go through\n\nWe could not charge your card for DiscoNotify on {{server_name}} ({{price}}/month). We will try again automatically, and alerts keep running meanwhile.\n\nTo use a different card: {{manage_url}}\n\nNeed help? Write to {{support_email}}.'),
('renewal_reminder', 'Upcoming payment reminder', 'Sent a few days before the next card payment is taken.', 'transactional',
 'Your DiscoNotify payment is coming up',
 '{{price}} will be charged on {{next_charge_date}}.',
 '<h1>Payment coming up</h1><p>Your DiscoNotify subscription for <strong>{{server_name}}</strong> renews on <strong>{{next_charge_date}}</strong> — {{price}} will be charged to your card.</p><p>To change your card or cancel first, open <a href="{{{manage_url}}}">your billing page</a>.</p>',
 E'Payment coming up\n\nYour DiscoNotify subscription for {{server_name}} renews on {{next_charge_date}} - {{price}} will be charged to your card.\n\nChange your card or cancel first: {{manage_url}}'),
('trial_ended_reminder', 'Trial ended — upgrade reminder', 'Sent up to 3 times, a week apart, after a free trial ends without a subscription.', 'marketing',
 'Your DiscoNotify trial has ended',
 'Alerts for {{server_name}} are paused — subscribe to switch them back on.',
 '<h1>Your free trial has ended</h1><p>Alerts for <strong>{{server_name}}</strong> are paused, but every setting is kept — nothing needs setting up again.</p><p><a href="{{{subscribe_url}}}">Subscribe for {{price}}/month</a> and alerts resume within a couple of minutes.</p><p style="color:#666;font-size:12px">Reminder {{reminder_number}} of {{reminder_total}}.</p>',
 E'Your free trial has ended\n\nAlerts for {{server_name}} are paused, but every setting is kept.\n\nSubscribe for {{price}}/month and alerts resume within a couple of minutes: {{subscribe_url}}\n\nReminder {{reminder_number}} of {{reminder_total}}.')
on conflict (key) do nothing;

-- ── Access ───────────────────────────────────────────────────────────
alter table disconotify.notify_email_contacts enable row level security;
alter table disconotify.notify_email_templates enable row level security;
alter table disconotify.notify_email_template_versions enable row level security;
alter table disconotify.notify_email_outbox enable row level security;

grant all on disconotify.notify_email_contacts to service_role;
grant all on disconotify.notify_email_templates to service_role;
grant all on disconotify.notify_email_template_versions to service_role;
grant all on disconotify.notify_email_outbox to service_role;
