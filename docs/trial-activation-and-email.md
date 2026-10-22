# Trial activation & email — what the website must do

This is the website's half of two features (the bot's half is built; see the
README, "Payments setup"). The admin panel's half is in
[admin-panel-email.md](admin-panel-email.md).

1. **Trial activation.** A *new* server's free trial no longer starts when the
   bot joins. The admin who added the bot gets a DM with a link to
   `/activate` on the website. There they log in with Discord, pick the
   server, and the trial starts — and the website learns their **verified
   email**, which is where every later billing and reminder email goes.
2. **Email.** Billing and reminder messages are emailed instead of DMed. The
   website's part: collect the email (activation and checkout), and provide the
   `/unsubscribe` and `/manage` pages that the emails link to.

Servers that already existed keep their trial exactly as it is and never need
to activate (they *may* — the same page then only saves their email, and never
touches their dates).

```
Admin adds the bot ──► bot DMs the admin: "Activate your free trial → /activate"
                                   │  (one follow-up DM 24 h later if still not done)
                                   ▼
GET /activate        Login with Discord (identify + email + guilds)
                     list: servers you manage that the bot is in, with their state
                                   │ pick one
                                   ▼
POST /api/activate   check the session + that email is Discord-verified
                     → database function notify_activate_trial(...)
                                   │
                                   ▼
        trial starts · email saved as a contact · "trial activated" email queued
        → bot sends it within ~1 minute; every command in that server starts working
```

Until a server is activated, **every `/notify` command except `help` and
`status` answers with the activation link**, and monitoring is paused (nothing
is set up yet, so nothing is lost).

## 1. Settings

| Setting | Value |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Already used for the "activating your server…" step. Server side only. Schema `disconotify` must be in the API's *exposed schemas*. |
| `BILLING_LINK_SECRET` | The **same** string as the bot's (signs/verifies unsubscribe links too). Server side only. |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET` | As for the server picker ([website-integration.md](website-integration.md) §2B). |
| `TRIAL_DAYS` | **Must equal the bot's `BILLING_TRIAL_DAYS`** (7). It's passed to the database function. |
| `POLAR_PORTAL_URL` | Your Polar customer-portal address, e.g. `https://polar.sh/<org-slug>/portal`, for the `/manage` redirect. |
| `SUPPORT_EMAIL` | Shown on the pages. |

Add the `email` scope to the Discord login: `scope=identify email guilds`.
Tell people why in the Privacy Policy (§7).

## 2. `GET /activate` — the page

Reuses the Discord login and server picker from
[website-integration.md](website-integration.md) §2B, with these changes:

- **Scopes:** `identify email guilds`. From `GET /users/@me` keep `id`,
  `email` and `verified`. **Discard the Discord token right after.**
- **Refuse an unverified or missing email** (`verified !== true` or no
  `email`): show *"Discord has no verified email for your account. Verify it in
  Discord's settings (User Settings → My Account), then try again."* Don't let
  people type another address in v1.
- **The server list** — the servers they can manage (owner, **Manage Server**
  or **Administrator**) where the bot is present
  (`disconotify.notify_guilds.bot_present = true`). For each, read
  `disconotify.notify_billing` and show one state:

  | Condition | Show | Button |
  |---|---|---|
  | no row, or `activation_required = true` and `trial_ends_at is null` | "Free trial not started" | **Start free trial** |
  | `whitelisted` | "Whitelisted — nothing to pay" | none |
  | `now < paid_until` | "Active until …" | none (manage link) |
  | `now < trial_ends_at` | "Free trial — N days left" | **Save my email** (optional) |
  | otherwise | "Trial ended" | **Subscribe** (→ `/subscribe`) |

  Show the new-server case first; if there is exactly one such server,
  pre-select it.
- Copy near the button: *"Your trial starts now and lasts 7 days. We'll use
  **you@example.com** for billing and reminder emails. You can unsubscribe from
  reminders any time."*

## 3. `POST /api/activate`

Body: `{ guild_id }`. **Never trust the browser's server ID** — check it
against the list stored in the server-side session when they logged in.

```js
// Node + @supabase/supabase-js, service key, schema 'disconotify'
const { data, error } = await supabase
  .schema('disconotify')
  .rpc('notify_activate_trial', {
    p_guild_id: guildId,          // a string — it exceeds JS's safe integer range
    p_user_id: session.userId,    // a string too
    p_email: session.email,       // the verified Discord email from the session
    p_trial_days: Number(process.env.TRIAL_DAYS),
  });
```

The function is atomic and safe to call repeatedly. It returns
`{ status, trial_ends_at, whitelisted }`:

| `status` | Meaning | Show |
|---|---|---|
| `activated` | The trial started now | ✅ "Your free trial has started and runs until **{date}**. We've emailed you a confirmation. Alerts work in your server right away." |
| `email_saved` | The server already had a trial; dates untouched | ✅ "Thanks — we'll email you about your trial and billing." (Show the existing end date.) |

Errors (`error.message`): `the bot is not in this server` → "Add the bot to the
server first" (+ the invite link); `invalid email` → ask them to verify their
Discord email. Anything else → a generic "something went wrong" (log the detail
server-side only). The function can't be called with the anon key: only
`service_role` may execute it.

After `activated` the bot notices on its next check (within about a minute) —
no extra call is needed. The "trial activated" email is already queued by the
function.

## 4. Email addresses — rules

- Store lowercase. One server can have several contacts (several admins);
  every contact gets that server's emails.
- Don't show other contacts' addresses to anyone. In the UI mask them
  (`j***@gmail.com`).
- Billing emails (receipts, failed payment, cancellation, payment coming up)
  always go to every contact. The trial-ended upgrade reminder is *marketing*:
  it carries an unsubscribe link and skips anyone who unsubscribed.

## 5. Checkout (paying customers) — save the email as a contact

You already collect the email before payment. Make sure it lands in the same
list, **before** creating the Polar checkout or the crypto payment:

```js
await supabase.schema('disconotify').from('notify_email_contacts').upsert(
  { guild_id: guildId, email: email.trim().toLowerCase(), discord_user_id: session.userId, source: 'checkout' },
  { onConflict: 'guild_id,email', ignoreDuplicates: true },   // never clears an unsubscribe
);
```

- Prefill the Polar checkout's customer email with the same address.
- Use the email Polar/the form gives you for payments; if it's the Discord email
  the row simply already exists.
- The *success* page should say: "A confirmation email is on its way to
  **{email}**."
- The bot queues "subscription started" itself once it sees the payment — no
  extra call. (It never DMs payment confirmations any more; a payment started
  from `/notify subscribe` in a channel is still confirmed in that channel.)

## 6. Pages the emails link to

### `GET /unsubscribe?t=<token>` (+ `POST`)

The reminder email's footer link, and the one-click `List-Unsubscribe` header
(mail apps send a `POST` to the same URL).

- **Verify the token** (HMAC-SHA256, same secret and format as the subscribe
  token):
  ```
  token   = payload + "." + signature
  payload = base64url(JSON, no padding)  → {"v":1,"k":"unsub","e":"<email>","g":"<server id>","x":<expiry unix s>}
  sig     = base64url(HMAC-SHA256(secret, payload)), no padding   // over the payload string exactly as in the token
  ```
  Reject if the signature is wrong (use a constant-time compare), `k !== 'unsub'`,
  or `x` is in the past (tokens last a year).
- **GET** → show the address and a **"Unsubscribe from reminder emails"**
  button. Don't unsubscribe on GET: mail scanners and link previewers open
  links, and would unsubscribe people by accident.
- **POST** (the button, or a mail app's one-click) → do:
  ```sql
  update disconotify.notify_email_contacts
     set marketing_unsubscribed_at = now()
   where email = lower($1) and marketing_unsubscribed_at is null;
  ```
  (every server that address is a contact for), then show: "Done — you won't
  get reminder emails any more. Billing emails about your subscription (receipts,
  failed payments) will still reach you." Offer **"Undo"** (sets it back to
  `null`) — it requires the same token.
- Never reveal whether an address exists; a bad token gets a neutral page.

### `GET /manage`

What the emails call "your billing page". The simplest correct version:
**redirect (302) to `POLAR_PORTAL_URL`** — Polar's own customer portal, where
the customer enters their email, receives a code, and can update the card or
cancel. (This avoids giving the website a token that can open other people's
portals.) Put a line above the redirect or on a short interstitial: "You'll
manage your subscription on Polar, our payment provider."

### `/thanks` (after payment)

Add the "confirmation email" line from §5. Everything else is unchanged.

## 7. Legal text to add

- **Privacy Policy:** Discord login (`identify`, `email`, `guilds`) is used to
  show the servers you manage, start your trial and contact you about your
  subscription; the Discord token is discarded straight away; emails are kept
  while the server uses the service, and deleted on request; the email
  provider (the mail host for disconotify.com) processes them; billing emails can't be unsubscribed from
  while a subscription exists, reminder emails can.
- **Terms:** the free trial starts when you activate it; a server that hasn't
  activated has no alerts.
- The emails' footer needs a **postal address** in some countries
  (CAN-SPAM, etc.) — set it in the admin panel's email layout (footer).

## 8. Database — what the website reads and writes

Run `0013_email_and_trial_activation.sql` first (the bot's migration).

| Table / function | Website access |
|---|---|
| `notify_activate_trial(...)` | **call** (service key) — activation |
| `notify_email_contacts` | **upsert** at checkout; **update** `marketing_unsubscribed_at` on unsubscribe |
| `notify_billing` | **read** (server states for the picker); never write |
| `notify_guilds` | **read** (`bot_present`) |
| `notify_email_outbox`, `notify_email_templates` | not touched (the bot and admin panel own them) |

Row-level security stays on; the service key bypasses it, so **never expose it
to the browser** and keep it out of logs and client bundles.

## 9. Test checklist

- A brand-new server: before activating, `/notify account-add` in Discord shows the activation link; after `POST /api/activate` it works within about a minute, and the "trial activated" email arrives.
- Activating twice changes nothing (`email_saved` the second time, same dates).
- An older server (has a trial): `/activate` saves the email and the trial end date is **unchanged**.
- Someone without Manage Server cannot activate a server (tamper with `guild_id` in the request).
- Unverified Discord email → refused with the explanation.
- Unsubscribe link: GET changes nothing; POST does; a tampered or expired token is refused; undo works.
- Checkout with a new email → a `notify_email_contacts` row with `source = 'checkout'`, and an existing unsubscribed row stays unsubscribed.
