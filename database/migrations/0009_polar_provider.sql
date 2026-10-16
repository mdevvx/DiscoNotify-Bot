-- PulseNotify: 0009_polar_provider
--
-- Card payments moved from Paddle to Polar: payments ledger rows for card
-- subscriptions now have provider 'polar' (external_id = the Polar
-- subscription ID). Old 'paddle' rows stay valid.
--
-- Run after 0008_website_billing.sql.

do $$
declare constraint_name text;
begin
    for constraint_name in
        select conname
        from pg_constraint
        where conrelid = 'pulsenotify.pulsenotify_payments'::regclass
          and contype = 'c'
          and pg_get_constraintdef(oid) like '%provider%'
    loop
        execute format('alter table pulsenotify.pulsenotify_payments drop constraint %I', constraint_name);
    end loop;
end $$;

alter table pulsenotify.pulsenotify_payments
    add constraint pulsenotify_payments_provider_check
    check (provider in ('paddle', 'polar', 'nowpayments'));
