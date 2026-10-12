-- PulseNotify: 0005_move_to_pulsenotify_schema
--
-- Moves every PulseNotify object out of `public` into a dedicated
-- `pulsenotify` schema, so this bot's tables are isolated from anything
-- else living in the same Supabase project. Run after 0004.
--
-- ALTER ... SET SCHEMA moves objects in place: data, indexes, constraints,
-- foreign keys, triggers, RLS settings, and columns typed with the domains
-- all come along untouched (Postgres tracks them by OID, not by name).
--
-- Idempotent: each move is skipped if the object is already gone from
-- `public`, so re-running this, or running it on a fresh project where
-- 0001-0004 just created everything in `public`, is safe.
--
-- AFTER RUNNING THIS, expose the schema to the Data API, otherwise every
-- query from the bot fails with "Invalid schema: pulsenotify":
--   Dashboard > Project Settings > Data API > Exposed schemas > add `pulsenotify`

create schema if not exists pulsenotify;

do $$
declare
    obj text;
begin
    foreach obj in array array[
        'pulsenotify_guilds',
        'pulsenotify_bot_settings',
        'pulsenotify_platform_accounts',
        'pulsenotify_notification_channels',
        'pulsenotify_account_channels',
        'pulsenotify_alert_configurations',
        'pulsenotify_events',
        'pulsenotify_live_states',
        'pulsenotify_audit_logs',
        'pulsenotify_platform_health'
    ] loop
        if to_regclass('public.' || obj) is not null then
            execute format('alter table public.%I set schema pulsenotify', obj);
        end if;
    end loop;

    foreach obj in array array[
        'platform_t',
        'event_type_t',
        'account_status_t',
        'platform_health_status_t'
    ] loop
        if to_regtype('public.' || obj) is not null then
            execute format('alter domain public.%I set schema pulsenotify', obj);
        end if;
    end loop;

    if to_regprocedure('public.pulsenotify_set_updated_at()') is not null then
        alter function public.pulsenotify_set_updated_at() set schema pulsenotify;
    end if;
end$$;

-- The bot connects with the secret/service_role key. service_role bypasses
-- RLS but still needs ordinary privileges on a non-public schema — Supabase
-- only pre-grants those on `public`. anon/authenticated get nothing: no
-- end-user client ever talks to these tables.
grant usage on schema pulsenotify to service_role;
grant all on all tables in schema pulsenotify to service_role;
grant all on all sequences in schema pulsenotify to service_role;
grant all on all routines in schema pulsenotify to service_role;

-- Covers tables added by future migrations without repeating the grants.
alter default privileges in schema pulsenotify grant all on tables to service_role;
alter default privileges in schema pulsenotify grant all on sequences to service_role;
alter default privileges in schema pulsenotify grant all on routines to service_role;
