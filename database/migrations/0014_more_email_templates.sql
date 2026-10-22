-- DiscoNotify: 0014_more_email_templates
--
-- Three more emails, so every moment that matters to a paying customer is
-- covered from DiscoNotify's own address (not only by the payment provider's):
--   subscription_renewed         — a card payment for the next month went through
--   subscription_ended           — a card subscription ended; alerts are paused
--   subscription_lapsed_reminder — a paid subscription ran out (and its grace
--                                  period) without being renewed; up to 3,
--                                  a week apart, like the trial-ended reminder
-- Run after 0013_email_and_trial_activation.sql. Existing templates are untouched.

insert into disconotify.notify_email_templates (key, name, description, category, subject, preheader, body_html, body_text) values
('subscription_renewed', 'Subscription renewed', 'Sent when a card payment for the next month goes through.', 'transactional',
 'Your DiscoNotify subscription was renewed',
 '{{price}} received — active until {{paid_until}}.',
 '<h1>Thanks — your subscription was renewed</h1><p>Your {{price}} payment for <strong>{{server_name}}</strong> went through.</p><p>{{status_line}}</p><p>Your payment provider sends the official receipt separately. You can manage or cancel your subscription any time from <a href="{{{manage_url}}}">your billing page</a>.</p>',
 E'Thanks - your subscription was renewed\n\nYour {{price}} payment for {{server_name}} went through.\n\n{{status_line}}\n\nYour payment provider sends the official receipt separately. Manage or cancel any time: {{manage_url}}'),
('subscription_ended', 'Subscription ended', 'Sent when a card subscription ends and alerts are paused.', 'transactional',
 'Your DiscoNotify subscription has ended',
 'Alerts for {{server_name}} are paused — every setting is kept.',
 '<h1>Your subscription has ended</h1><p>The DiscoNotify subscription for <strong>{{server_name}}</strong> has ended, so alerts are paused. <strong>Every setting is kept</strong> — nothing needs setting up again.</p><p>To switch alerts back on, <a href="{{{subscribe_url}}}">subscribe again</a> ({{price}}/month); they resume within a couple of minutes.</p><p>Something wrong? Write to {{support_email}}.</p>',
 E'Your subscription has ended\n\nThe DiscoNotify subscription for {{server_name}} has ended, so alerts are paused. Every setting is kept - nothing needs setting up again.\n\nTo switch alerts back on, subscribe again ({{price}}/month): {{subscribe_url}}\n\nSomething wrong? Write to {{support_email}}.'),
('subscription_lapsed_reminder', 'Subscription lapsed — renew reminder', 'Sent up to 3 times, a week apart, after a paid subscription runs out without being renewed.', 'marketing',
 'Your DiscoNotify subscription has run out',
 'Alerts for {{server_name}} are paused — renew to switch them back on.',
 '<h1>Your subscription has run out</h1><p>Alerts for <strong>{{server_name}}</strong> are paused since {{paid_until}}, but every setting is kept.</p><p><a href="{{{subscribe_url}}}">Renew for {{price}}/month</a> and alerts resume within a couple of minutes.</p><p style="color:#666;font-size:12px">Reminder {{reminder_number}} of {{reminder_total}}.</p>',
 E'Your subscription has run out\n\nAlerts for {{server_name}} are paused since {{paid_until}}, but every setting is kept.\n\nRenew for {{price}}/month and alerts resume within a couple of minutes: {{subscribe_url}}\n\nReminder {{reminder_number}} of {{reminder_total}}.')
on conflict (key) do nothing;

-- The emails come from a no-reply address, so no template may say "reply to this email".
update disconotify.notify_email_templates
   set body_html = replace(body_html, 'Reply to this email or write to', 'Write to')
 where body_html like '%Reply to this email or write to%';
