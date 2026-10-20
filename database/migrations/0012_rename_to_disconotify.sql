-- DiscoNotify: 0012_rename_to_disconotify
--
-- Product rename PulseNotify -> DiscoNotify: schema `pulsenotify` becomes
-- `disconotify`, and every `pulsenotify_<name>` table becomes `notify_<name>`.
-- Data, foreign keys, RLS and grants are preserved (renames only). Indexes,
-- triggers and constraints keep their old names, which is harmless.
--
-- Run after 0011_dm_log.sql, then add `disconotify` under Project Settings >
-- Data API > Exposed schemas (and remove `pulsenotify`). Deploy the renamed
-- bot, website and admin panel at the same time as this migration.

do $$
declare
    t record;
begin
    if exists (select 1 from pg_namespace where nspname = 'pulsenotify')
       and not exists (select 1 from pg_namespace where nspname = 'disconotify') then
        alter schema pulsenotify rename to disconotify;
    end if;

    for t in
        select tablename from pg_tables
        where schemaname = 'disconotify' and tablename like 'pulsenotify\_%'
    loop
        execute format('alter table disconotify.%I rename to %I',
                       t.tablename, 'notify_' || substr(t.tablename, length('pulsenotify_') + 1));
    end loop;

    if to_regprocedure('disconotify.pulsenotify_set_updated_at()') is not null then
        alter function disconotify.pulsenotify_set_updated_at() rename to notify_set_updated_at;
    end if;
end $$;

grant usage on schema disconotify to service_role;
