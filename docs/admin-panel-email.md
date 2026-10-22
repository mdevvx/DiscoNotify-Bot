# Admin panel — Email section (templates, manual sends, log)

An addition to the DiscoNotify product in the admin panel hub
([admin-panel-prompt.md](admin-panel-prompt.md)): same shell, same auth, roles,
audit log, design system and security checklist. The bot sends every email; the
panel **edits what is sent** and **queues manual emails**. Website side:
[trial-activation-and-email.md](trial-activation-and-email.md).

All tables below are in schema `disconotify`. The panel uses the service key
server-side only (never in the browser), after `requireAdmin`.

## How the pieces fit

```
admin panel ── edits ─────────► notify_email_templates   (subject, HTML, text, on/off)
            ── edits ─────────► notify_bot_state keys     email_layout, email_schedule
            ── inserts ───────► notify_email_outbox        (manual emails, status 'queued')
bot (every ~60 s)  reads outbox ► renders with the CURRENT template ► sends from the @disconotify.com mailbox (SMTP) ► writes the result back
website / bot / panel ─ insert ► notify_email_outbox      (automatic emails, with a dedupe_key)
```

The bot renders at send time, so **saving a template changes every email that
is still queued**, and nothing the panel does can send an email directly. Sends
appear within about a minute; the panel shows the live status.

## Navigation

**Email** (sidebar group) → **Templates · Send email · Outbox · Contacts ·
Settings**. Plus changes to existing pages (last section).

## 1. Templates

### List
One row per template (`notify_email_templates`): name, key, category badge
(**Billing** = `transactional`, **Reminder / marketing** = `marketing`), enabled
toggle, subject, version, last edited (by, when), emails sent in the last 30 days
(count from the outbox). A template's **toggle** controls the *automatic*
emails only; manual sends of a switched-off template still work. Switching one
off needs a confirm ("Customers will stop receiving this email").

The built-in templates (the bot looks them up by `key`; the panel must not
let an admin delete them, rename the key, or change what triggers them):

| key | Category | Sent when |
|---|---|---|
| `trial_activated` | transactional | someone activates a server's trial on the website |
| `subscription_started` | transactional | a card subscription or crypto payment begins |
| `subscription_cancelled` | transactional | a card subscription is cancelled |
| `payment_failed` | transactional | a card payment fails (Polar retries it) |
| `renewal_reminder` | transactional | N days before the next card payment (or a trial turning into the first charge) |
| `subscription_renewed` | transactional | a card payment for the next month went through |
| `subscription_ended` | transactional | a card subscription ended and alerts are paused |
| `trial_ended_reminder` | marketing | after a free trial ends without a subscription; right away, then weekly, at most 3 |
| `subscription_lapsed_reminder` | marketing | the same series after a paid subscription runs out (and its grace period) |

Show each template's **trigger and timing** read-only next to the editor, so
nobody wonders when it goes out.

### Editor
Two-column: editor left, live preview right. Fields:

- **Name** and **description** (internal, shown in the list).
- **Subject** — single line, ≤ 150 characters, variables allowed, live character count (inboxes cut around 60 — show a soft warning).
- **Preheader** — the grey preview line next to the subject, ≤ 200 characters, variables allowed.
- **HTML body** — a code editor (syntax highlighting, line numbers) *and* a simple visual mode (headings, bold/italic, links, lists, button, divider, image by URL). The shared layout (header, footer, unsubscribe link) wraps it — don't include `<html>`/`<body>` and don't ask authors to build the shell.
- **Plain-text body** — required (some people read text only, and spam filters like it). A **"Generate from HTML"** button fills it; editing it afterwards is allowed.
- **Variables panel** — a clickable list that inserts `{{name}}` at the cursor, each with its meaning and sample value (table below). A second row inserts **link buttons** (`<a href="{{{subscribe_url}}}">…</a>`).
- **Category** — shown, read-only for built-ins (owner can change a custom one).
- **Enabled** toggle.

**Variables** (the bot ignores anything else — an unknown name renders as empty):

| Variable | Holds | Example |
|---|---|---|
| `{{server_name}}` | Discord server name | Gaming Hub |
| `{{price}}` | monthly price | $1.99 |
| `{{trial_days}}` | trial length | 7 |
| `{{trial_ends_at}}` | | October 29, 2026 |
| `{{paid_until}}` | | November 29, 2026 |
| `{{next_charge_date}}` | next card payment | November 29, 2026 |
| `{{status_line}}` | ready-made sentence about the subscription's state | Your subscription is active until … |
| `{{{subscribe_url}}}` | subscribe page | https://disconotify.com/subscribe |
| `{{{activate_url}}}` | trial activation page | https://disconotify.com/activate |
| `{{{manage_url}}}` | manage/cancel page | https://disconotify.com/manage |
| `{{{unsubscribe_url}}}` | unsubscribe link (marketing only; empty otherwise) | … |
| `{{support_email}}` | | support@disconotify.com |
| `{{reminder_number}}` / `{{reminder_total}}` | 1, 2, 3 / 3 | |
| `{{brand_name}}` | from the layout | DiscoNotify |

Rules the editor enforces (and the server re-checks on save):

- **Escaping.** `{{name}}` is HTML-escaped in the body; `{{{name}}}` (three braces) inserts raw HTML **only for the four `*_url` variables** — put them in `href="…"`. Any other name in triple braces is still escaped. Explain this in a help popover.
- **Unknown variables** (anything not in the table) → blocking error naming the variable and the line ("`{{servername}}` — did you mean `{{server_name}}`?").
- **Required.** Subject, HTML and text must be non-empty. A `marketing` template needs no unsubscribe text of its own (the layout adds the link), but warn if the body mentions "unsubscribe" without the variable.
- **Size.** HTML ≤ 100 KB. Warn on > 1 MB-equivalent images, inline `<style>` that mail clients drop, `<script>`/`<iframe>`/`<form>`/`<video>` (blocked — strip server-side), event handlers (`onclick=` …), and `javascript:` URLs.
- **Links** must be `https://` (or `mailto:`), or a `{{{…_url}}}` variable.
- **Sanitize on save** (an allow-list HTML sanitizer: headings, paragraphs, lists, tables, links, images, basic inline styles). Show the author what was removed.

### Preview
- Devices: **Desktop / Mobile**; modes: **Light / Dark**; tabs: **HTML / Plain text / Raw source**; and a **"Subject + preheader as seen in an inbox"** card.
- Data: pick **sample data** (a normal one, a very long server name, a name with `<b>&"` characters, no trial date) or **"use a real server"** (search, then fill the variables from its billing row). Missing/empty variables are highlighted.
- Render **in a sandboxed iframe** (`sandbox=""`, `srcdoc`) — never inject template HTML into the panel's own DOM.
- The preview must call the **same rendering rules as the bot** (escaping, triple-brace links, layout). Keep one shared implementation or golden-file tests against these cases: `{{server_name}}` = `<b>x</b>` is escaped; `{{{server_name}}}` is still escaped; `{{{subscribe_url}}}` inside `href` is raw; unknown names vanish.

### Test send
"Send me a test" → renders with the chosen sample/real data and queues it to **the admin's own address** (outbox row, `kind = 'manual'`, `created_by` = their email, subject prefixed `[TEST] `). Rate limit 5 per 10 minutes. Allowed for every editor.

### Save, history, restore
- **Save** writes the row (`subject`, `preheader`, `body_html`, `body_text`, `enabled`, `updated_by` = admin email). The database **automatically snapshots** the previous content into `notify_email_template_versions` and bumps `version`.
- **History** tab: versions (number, who, when), a **diff** view (side by side, HTML and text), **Restore** (copies that version's content into the template — creating a new version) with a confirm. Restore never deletes history.
- **Unsaved changes** guard, autosave of the draft in the browser (local storage, per template) with "restore draft?".
- **Optimistic concurrency:** send the `version` you loaded; if it changed, show "Someone else edited this — view their changes" instead of overwriting.
- Every save/toggle/restore → `hub_audit_log` with before/after (subject + a hash/size of bodies is enough; store the full diff for the first 10 KB).

## 2. Layout & branding (shared by every email)

One form, stored as JSON in `notify_bot_state` under key **`email_layout`**:

```json
{ "brand_name": "DiscoNotify", "logo_url": "https://…/logo.png", "accent_color": "#5B6CF0",
  "footer_html": "This is an automated message — replies aren't monitored. Need help? Write to {{support_email}}.<br>DiscoNotify · 123 Example Street, City, Country",
  "footer_text": "This is an automated message - replies aren't monitored. Need help? Write to {{support_email}}.\nDiscoNotify · 123 Example Street, City, Country" }
```

- **Every email is sent from `no-reply@disconotify.com` and can't be replied to** — the panel must not offer a sender or reply-to field, and template copy must never say "reply to this email" (say "write to {{support_email}}" instead; warn in the editor if the text contains "reply to this").
- Fields: brand name, logo (https URL, ≤ 200 px tall, with alt text = brand name), accent colour (hex, with a contrast check against white), footer (HTML + text; allowed variables: `support_email`, `brand_name`). **The footer should contain the company's postal address** (required in several countries). The unsubscribe link is added by the bot automatically on marketing emails — don't add it by hand.
- Live preview on a sample template. Same sanitizer rules. Owner role only.
- Missing keys fall back to the bot's defaults, so a partial object is fine.

## 3. Settings — schedule

Stored as JSON in `notify_bot_state` under key **`email_schedule`**:

```json
{ "renewal_days_before": 3, "trial_reminder_every_days": 7, "trial_reminder_max": 3 }
```

| Field | Range | Meaning |
|---|---|---|
| `renewal_days_before` | 0–30 integer | "payment coming up" email this many days before a card charge; **0 = never send it** |
| `trial_reminder_every_days` | 1–60 | gap between trial-ended reminders |
| `trial_reminder_max` | 0–10 | how many in total; 0 = none |

- Integers only; anything else is ignored by the bot (it falls back to its own configuration), so validate strictly.
- Show the **effective value** and where it comes from (this setting, or "bot default": 3 / 7 / 3).
- The change applies on the bot's next check (within ~10 minutes). Show an info banner saying so. Existing reminder counts per server are untouched.
- Also show, read-only: "Email provider connected?" — *not* something the panel can know directly; show the **last successful send** time and the failure rate of the last 24 h from the outbox instead, with a red banner when `failed` rows pile up ("check the SMTP settings and the mailbox's password — a mail-server login error shows in the rows' `status_detail`").

## 4. Send email (manual)

For anything the automatic emails don't cover — a one-off notice, an apology, a
personal follow-up, re-sending something.

**Step 1 — Recipients** (one of):
- **A server**: search by name/ID → shows its contacts (masked) as checkboxes (all ticked).
- **Several servers** by filter: state (trial / trial ended / active / card past due / lapsed / not activated), created between dates, has contacts. Shows **"N servers · M emails"** before going on.
- **Specific addresses**: paste a list (validated, de-duplicated; each is sent with no server, so server variables are empty).
- Always show: how many recipients **will be skipped** and why (no email, unsubscribed from marketing).

**Step 2 — Content** (one of):
- **Use a template** (any, including switched-off ones). Optionally override the subject. Optionally edit variables (`server_name` is filled per server automatically; extra values apply to everyone).
- **Custom email**: subject + HTML + plain text (same editor, sanitizer and variable rules as templates). A **"This is a reminder/marketing email"** checkbox — when ticked, the email carries the unsubscribe link and **skips people who unsubscribed** (the panel writes `variables.marketing = true`). Default for bulk sends: ticked.

**Step 3 — Review & send**
- Preview for the first recipient (same preview component), recipient list, the subject.
- **Send a test to me first** (strongly encouraged; required for > 20 recipients).
- **When**: now, or schedule (`send_after`, up to 30 days ahead; scheduled emails can be cancelled while `queued`).
- **Confirm**: single recipient → one click; 2–20 → confirm dialog with the count; > 20 → type `SEND 57` (the exact count) + a **mandatory reason**; **owner/admin role only** for > 20; step-up re-authentication for > 100.
- Rate limit: 200 manual recipients per admin per hour.

**What the panel writes** — one `notify_email_outbox` row per recipient:

| Column | Value |
|---|---|
| `to_email` | lowercase address |
| `guild_id` | the server, or `null` for a pasted address |
| `template_key` | the template's key, or `null` for a custom email |
| `kind` | `'manual'` |
| `variables` | JSON object: the variables (`server_name` …) plus `"marketing": true` if ticked. Dates as ISO strings (the bot formats them). |
| `subject_override` | the subject if overridden; **required** for a custom email |
| `body_html`, `body_text` | the custom content (custom emails; or optional overrides of a template's body) |
| `send_after` | `now()` or the scheduled time |
| `dedupe_key` | `null` (a manual email may be sent twice on purpose) |
| `created_by` | the admin's email |
| `status` | `'queued'` (the default) |

Then show a **live progress page** (poll every 5 s): queued / sent / failed /
skipped counts, with the failure reasons. Write one audit-log entry for the whole
send (recipient count, template or subject, reason).

## 5. Outbox (the email log)

Every email ever queued, newest first (`notify_email_outbox`). Server-side
pagination (50/page), URL-encoded filters:

- Status (`queued`, `sending`, `sent`, `failed`, `skipped`), template, kind (`auto` / `manual`), server, recipient (exact), date range, "sent by".
- Columns: when, to (masked), server, template (or "custom"), kind, status (colour **and** icon/label), subject as sent, attempts.
- **Detail drawer:** all fields, `status_detail` (why it failed or was skipped — e.g. `unsubscribed`, `template is switched off`, `The mail server answered 550: …`, `The mail server refused the login …`), `provider_id` (the message's `Message-ID`, useful when searching the mailbox's Sent folder), the variables used, and **"View as sent"** — re-rendered from the template's *current* version with the stored variables (say so: it may differ from what was sent if the template changed).
- **Actions** (role-gated, audit-logged, confirmed):
  - **Retry** a `failed` row: set `status='queued'`, `attempts=0`, `send_after=now()`.
  - **Cancel** a `queued` row: `status='skipped'`, `status_detail='cancelled by <admin email>'`.
  - **Send again** (copy as a new manual email).
- A banner counts `queued` rows older than 15 minutes ("the bot isn't sending — is it online, and are the `EMAIL_SMTP_*` settings complete?").
- CSV export: owner only, audit-logged, masked unless the owner ticks "include full addresses".

Never store or show email *bodies* anywhere except by re-rendering — the outbox
keeps only variables, subject and status, on purpose.

## 6. Contacts

`notify_email_contacts` — one row per (server, address).

- Search by address or server. Columns: address (masked, reveal with a click that is audit-logged), server, source (`activation` / `checkout` / `manual`), Discord user, **marketing: subscribed / unsubscribed (date)**, added.
- **Add contact** to a server (source `manual`) and **remove** one — both confirmed + audit-logged.
- **Re-subscribe** someone who unsubscribed (sets `marketing_unsubscribed_at` to `null`): owner only, mandatory reason, and a warning ("only if they asked you to"). Unsubscribing on someone's behalf: the same, reversed.
- Show a **suppression summary**: total contacts, unsubscribed count.
- Right-to-erasure: "Delete this address everywhere" (owner only, confirmed): deletes contact rows and outbox rows for that address.

## 7. Changes to existing pages

- **Server detail** gets: an **Activation** block (`activation_required`, `trial_activated_at`, `activated_by`, `invited_by`, with a note when the trial hasn't started: "alerts are paused until the admin activates it on the website"), a **Contacts** card (masked, with *Add*), an **Email timeline** (the last 20 outbox rows), and the actions **Send email to this server** and **Resend last email**. The existing action "extend trial by N days" is only available once the trial has started (`trial_ends_at` not null); for a server that never activated, show "Not activated yet".
- **Servers list**: new state filter **Not activated** (`activation_required = true and trial_ends_at is null`), a "has email" column, and an "emails failed" badge.
- **Reminders & alerts** (section 6 of the main prompt) is now email-based: replace the DM counts with email counts per type, and the "send a reminder now" action with **Send email to this server** (this section's manual send). Keep the billing events feed.
- **Customers**: the "Email addresses are not collected yet" note goes away — show contact addresses (masked) per customer.
- **DM log** now only contains the two trial-activation DMs (`kind` = `activation`, `activation_reminder`); label it "Activation DMs".
- **Settings (read-only view)** gains the email schedule and the "email provider status" line from §3.
- **Audit**: add the new actions (`email.template.save|toggle|restore`, `email.layout.save`, `email.schedule.save`, `email.send` (bulk, with count + reason), `email.outbox.retry|cancel`, `email.contact.add|remove|resubscribe|erase`, `email.export`).

## 8. Roles

| Action | support | admin | owner |
|---|---|---|---|
| View templates, outbox, contacts (masked) | ✅ | ✅ | ✅ |
| Send a test to self | ✅ | ✅ | ✅ |
| Edit / toggle / restore templates | — | ✅ | ✅ |
| Manual send to 1 server (≤ 20 recipients) | ✅ (billing templates only) | ✅ | ✅ |
| Manual send to > 20 recipients, custom emails | — | ✅ | ✅ |
| Retry / cancel outbox rows | ✅ | ✅ | ✅ |
| Reveal full addresses, add/remove contacts | — | ✅ | ✅ |
| Edit layout, schedule; re-subscribe; erase; CSV export | — | — | ✅ |

## 9. Security & privacy (adds to the main checklist)

- Template/preview HTML only ever rendered in a **sandboxed iframe**; sanitize on save **and** when previewing; never `dangerouslySetInnerHTML` an admin-supplied string.
- The service key and any provider keys stay server-side. The panel never sends mail itself and never sees the SMTP password.
- Mask addresses everywhere by default; revealing/exporting is audit-logged. No addresses in URLs, query strings or application logs.
- zod-validate every input (lengths, URL schemes, integer ranges); reject unknown JSON keys when saving `email_layout` / `email_schedule`.
- Manual sends are rate-limited, require a reason when bulk, and are written to the audit log **in the same transaction** as the outbox inserts.
- Respect unsubscribes: the bot skips unsubscribed addresses for marketing emails *even for manual sends* — the panel should show the skip count up front so nobody is surprised.

## 10. Acceptance checklist

- Editing a template and saving creates a new version; restoring an old one works; two admins editing at once don't overwrite each other silently.
- Unknown variable → blocked with a helpful message; `<script>` is stripped; `{{{server_name}}}` stays escaped in the preview and in the real email.
- The preview matches the delivered email (same escaping, layout, links) for the golden cases in §1.
- A test send reaches the admin's own inbox within ~2 minutes (bot online, SMTP settings complete) and is labelled `[TEST]`.
- A manual send to a server with 2 contacts creates 2 outbox rows, shows live progress, and each row ends `sent` (or `skipped`/`failed` with a reason). A marketing manual send skips an unsubscribed contact and says so beforehand.
- Switching a template off stops the automatic email but not manual sends.
- Changing the schedule changes the next reminders (e.g. `renewal_days_before = 0` stops "payment coming up" emails).
- `support` cannot edit templates or bulk-send; only `owner` can edit layout/schedule or export.
- Every mutation above appears in the audit log; no secret or full address appears in the client bundle, URLs or logs.
