# Admin panel — build prompt

Hand everything below the line to the AI/engineer building the panel. It is self-contained.

---

## Task

Build **Hunter Hub**, a secure internal admin panel for my company. It is a **multi-product hub**: one login, and a product switcher (like workspaces) that changes the whole panel to that product's admin. The first product is **DiscoNotify** (a Discord bot that sends social-media alerts, billed per server). More products will be added later, so the hub must make adding a product cheap and must not hard-code DiscoNotify into the shell.

Admins only. Nobody else can sign in, ever.

## Tech stack (fixed)

- **Next.js** (App Router, TypeScript strict), **TailwindCSS**, **Supabase** — use the *existing* project and database; do not create a new one. Credentials come from env vars (below).
- Server Components and Route Handlers/Server Actions for everything that touches data. **The Supabase service-role key is used server-side only** and never reaches the browser bundle.
- Desktop only (min width 1280px; show a polite "open on a desktop" note below that). Light **and** dark theme, following the OS by default with a manual toggle, remembered per admin.
- Deployable on Vercel or Railway. No other paid services.

## Login and access (most important part)

1. **Allowlist only.** Table `hub_admins` (see data model). Signing in is possible only for an email that is in it and `active = true`. Public sign-up is **disabled** in Supabase Auth. An unknown email gets the same neutral message as a known one ("If this address is allowed, a sign-in link is on its way") so the allowlist can't be probed.
2. **Passwordless email link or code** through Supabase Auth, for allowlisted emails only (check the allowlist *before* asking Supabase to send anything — use a server action with the service key).
3. **Second factor required**: TOTP (Supabase MFA). First login forces enrolment; every session needs `aal2`. Offer 8 one-time recovery codes at enrolment.
4. **Sessions**: HttpOnly, Secure, SameSite=Lax cookies; 8-hour absolute lifetime, 30-minute idle timeout; "sign out everywhere" button. Re-ask MFA for dangerous actions older than 10 minutes (see "step-up").
5. **Every request is authorised on the server** — middleware plus a check inside each loader/action (`requireAdmin(role)`); never rely on hiding buttons. Deny by default.
6. **Roles** per admin, optionally per product: `owner` (manage admins, everything), `admin` (all actions in assigned products), `support` (read + limited actions), `viewer` (read only). Admins see only the products they are assigned.
7. Rate limit sign-in attempts (per IP and per email), lock for 15 minutes after 5 failures, and log every sign-in, failure and sign-out.
8. Security headers: strict CSP (no inline scripts except nonce), `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, HSTS, `Permissions-Policy` minimal. CSRF protection on all mutations (same-origin + token). Validate every input with zod. No `dangerouslySetInnerHTML`.
9. Never log or render secrets, tokens, or full payment-provider payloads. Mask personal data in lists.

## Hub shell

- Left sidebar: product switcher at the top (logo, name, health dot), then the selected product's navigation. Top bar: global search (⌘K / Ctrl+K command palette: jump to a product, a server, a customer, a payment), theme toggle, admin menu (profile, MFA, sign out).
- Products come from a registry, not code branches: table `hub_products` (`slug`, `name`, `description`, `icon`, `status`, `schema_name`). Each product is a folder `src/products/<slug>/` exporting `{ manifest, navigation, routes }`, and the shell renders whatever is registered. Adding a product = adding a folder + a row. Write a short `docs/ADDING_A_PRODUCT.md`.
- A hub **Home** page: one card per product with 3 headline numbers each (for DiscoNotify: servers with the bot, active subscriptions, MRR), plus a "needs attention" list across products.
- Hub-level pages: **Admins** (owner only: invite, change role/product access, deactivate, see last login), **Audit log** (every action by every admin, filterable, exportable to CSV), **Settings** (theme default, session lengths read-only).

## DiscoNotify product — pages and functions

Data lives in the Postgres schema `disconotify` of the same Supabase project (use the `Accept-Profile`/`Content-Profile` header or `db: { schema: 'disconotify' }` in the server client). Introspect it first; the repo's `database/migrations/*.sql` is the source of truth. Key tables:

| Table | What it holds |
|---|---|
| `notify_guilds` | Discord servers: `guild_id`, name, `bot_present`, settings, timestamps |
| `notify_billing` | One row per server: `trial_ends_at`, `paid_until`, `whitelisted` (+`whitelist_note`, `whitelisted_by`, `whitelisted_at`), card fields `card_subscription_id`/`card_customer_id`/`card_status`/`card_cancels_at`, `reminders_sent`, `last_reminder_at` |
| `notify_payments` | Ledger: `provider` (`polar` or `nowpayments`), `external_id`, `months`, `amount_usd`, `status` (pending/paid/failed/expired), `requested_by`, `channel_id`, `created_at`, `paid_at` |
| `notify_platform_accounts`, `notify_notification_channels`, `notify_alert_configurations`, `notify_events`, `notify_live_states` | What each server monitors and what was sent |
| `notify_platform_health`, `notify_audit_logs`, `notify_bot_state` | Platform status, bot-side audit trail, maintenance flag |

All IDs are Discord snowflakes (bigint) — **treat them as strings in JavaScript** (they exceed the safe integer range). Show them copyable.

Pages:

1. **Overview** — active subscriptions (card / crypto / whitelisted / trial), MRR, new this week, churn this month, trials ending in 3 days, servers where the bot is present, platform health strip (YouTube, Kick, Instagram, Twitter/X), maintenance mode status, last 10 payments. A small, calm chart (30-day paid revenue, 30-day new servers) — no chart junk.
2. **Servers** — table with search (name or ID), filters (state: trial / active / lapsed / whitelisted / card past due / bot removed), sort, pagination (server-side, 50/page). Row → **Server detail**: owner Discord ID, subscription state with dates, payment history, reminders sent (DM count, last email time), monitored accounts per platform, notification channels, recent events, audit trail. Actions (role-gated, confirmed, audit-logged): extend trial by N days, add/subtract paid days (with required reason), whitelist / remove whitelist (with note), mark bot removed.
3. **Customers** — one row per Discord user who paid (`requested_by`): Discord ID, servers they paid for, lifetime paid. Detail page shows everything about them. (Email addresses are not collected yet; when they are, they'll appear here — keep the layout ready for it.)
4. **Payments** — ledger with filters (provider, status, date range, amount), totals, CSV export, link each row to its server and, for card payments, to the Polar dashboard (`https://polar.sh/dashboard/...` — build the link from `card_subscription_id`; open in new tab). Read-only here: refunds and cancellations are done in Polar/NOWPayments; the panel never calls them with write permissions.
5. **Subscriptions health** — a work list: card `past_due`, cancelled-but-still-active (ends soon), crypto payments stuck `pending` > 1h, payments marked `failed` (e.g. underpaid), servers with two card subscriptions, sandbox leftovers (rows with card fields but no matching live payment).
6. **Reminders & alerts** — upcoming and recent upgrade reminders (DM) per server, plus a feed of billing events (renewals, failed charges, cancellations, failed crypto payments), a "send a reminder now" action for one server (rate limited; writes `reminders_sent`/`last_reminder_at` so the bot's schedule stays consistent — or simply resets them to trigger the bot; document which).
7. **Platform health** — per-platform status from `notify_platform_health`, quota use, error counts, last successful poll; plus a **maintenance mode** toggle (writes `notify_bot_state`, owner/admin only, confirmation + step-up).
8. **Audit** — the product's slice of the hub audit log plus `notify_audit_logs` from the bot, merged by time.
9. **Settings (read-only view)** — price, trial days, grace days, reminder schedule as the bot currently runs them (stored in `notify_bot_state` or shown as "configured on the bot's host"; don't build editing for env vars).

Every list: server-side pagination, URL-encoded filters (shareable links), empty states, loading skeletons, error states with a retry — and a "last updated" stamp.

## Data model the hub adds (create as a migration, schema `hub`)

- `hub_admins` (`id uuid pk` = Supabase auth user id, `email unique`, `display_name`, `role`, `active bool`, `created_by`, `created_at`, `last_login_at`)
- `hub_admin_products` (`admin_id`, `product_slug`, `role`) — per-product role override/assignment
- `hub_products` (see above)
- `hub_audit_log` (`id`, `at`, `admin_id`, `admin_email`, `product_slug`, `action`, `target_type`, `target_id`, `before jsonb`, `after jsonb`, `ip`, `user_agent`, `reason`) — **append-only** (no update/delete grants, RLS on)
- RLS on **every** table; the browser never queries these tables directly. The service key does, server-side, after `requireAdmin`.

## Security checklist (reject the build if any item is missing)

- Service-role key and provider tokens only in server env, never `NEXT_PUBLIC_*`.
- Allowlist + MFA enforced server-side; deny by default; roles enforced in every action.
- All mutations: zod validation, role check, step-up for dangerous ones (whitelist, add paid days, delete contact, maintenance, admin management), written to `hub_audit_log` with before/after in the same transaction.
- Dangerous actions need a typed confirmation ("type the server name") and a mandatory reason.
- No PII in URLs or logs; personal data masked in lists; CSV exports are audit-logged and owner/admin only.
- Dependency audit clean, lockfile committed, `npm run lint`, `typecheck`, and tests in CI.

## Design

- Calm, professional, **never harsh**: no pure white (#fff) page or pure black (#000) page, no neon, no heavy saturation, no thick borders. Light theme: soft warm-grey canvas (≈ #F4F5F7), surface slightly lighter, text ≈ #1F2430 (not black). Dark theme: deep blue-grey (≈ #14171E canvas, #1B1F29 surface, #262B38 raised), text ≈ #E4E7EE (not white). Define colours as CSS variables / Tailwind tokens so both themes share one component set.
- One quiet accent (muted indigo, ≈ #5B6CF0 light / #8794F5 dark), used for primary actions and focus rings only. Status colours softened and always paired with an icon or label (never colour alone): success muted green, warning muted amber, danger muted red, info muted blue. Contrast ≥ WCAG AA (4.5:1 for text) in both themes — check it.
- Typography: Inter (or system UI) at 14px base for dense tables, tabular numbers for money and counts, 8px spacing grid, 8–10px radius, 1px hairline borders, very light shadows.
- Dense but breathable data tables (sticky header, row hover, keyboard navigation), a proper side-sheet for quick views, toasts for results, skeletons for loading. Dark/light chart palettes derived from the same tokens.
- Keyboard first: ⌘K palette, `g` then `s` to go to servers, etc., with a `?` shortcut sheet.

## Project structure (suggested)

```
src/
  app/(auth)/login, mfa            # public, minimal
  app/(hub)/layout.tsx             # shell: sidebar, switcher, topbar
  app/(hub)/page.tsx               # hub home
  app/(hub)/p/[product]/...        # resolves to src/products/<slug>
  app/(hub)/admins, audit, settings
  products/disconotify/{manifest.ts,navigation.ts,pages/*,queries/*,actions/*}
  lib/{supabase-server.ts,auth.ts,audit.ts,roles.ts,validation.ts}
  middleware.ts
supabase/migrations/               # the hub schema above
docs/ADDING_A_PRODUCT.md, SECURITY.md
```

## Environment variables

`NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (only for the auth flow), `SUPABASE_SERVICE_ROLE_KEY` (server only), `HUB_URL`, `SESSION_SECRET`, `INITIAL_OWNER_EMAIL` (seeds the first `owner` on first deploy), optional `POLAR_DASHBOARD_ORG_SLUG` for deep links.

## Deliverables and acceptance

1. Working app with the hub shell, login + MFA, product switcher, and the full DiscoNotify product above.
2. The `hub` schema migration and a seed script for the first owner.
3. Tests: allowlist and role enforcement (unit), an end-to-end login → switch product → whitelist a server → see it in the audit log.
4. Docs: README (run, deploy, env), ADDING_A_PRODUCT.md, SECURITY.md (threat model, what each role can do).
5. Acceptance: a non-allowlisted email can never get in; an `support` admin cannot whitelist; every mutation appears in the audit log with before/after; no secret in the client bundle (verify with a bundle grep); both themes pass the contrast check; adding a dummy second product needs no change to the shell.
