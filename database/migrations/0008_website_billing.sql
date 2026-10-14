-- PulseNotify: 0008_website_billing
--
-- Payments move to the PulseNotify website (see README "Payments setup"):
-- the website — not a Discord user — now writes pending rows into
-- pulsenotify_payments, and the bot DMs server owners an upgrade reminder
-- when a trial or subscription runs out.
--
-- Run after 0007_bot_state.sql.

-- Website payments have no Discord user behind them.
alter table pulsenotify.pulsenotify_payments alter column requested_by drop not null;

-- Upgrade reminders: how many were sent for the current lapse, and when the
-- last one went out. Reset to 0 once the server has access again.
alter table pulsenotify.pulsenotify_billing add column if not exists reminders_sent integer not null default 0;
alter table pulsenotify.pulsenotify_billing add column if not exists last_reminder_at timestamptz;
