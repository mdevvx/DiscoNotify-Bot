# DiscoNotify

A multi-tenant Discord bot that watches social/streaming accounts (YouTube, Twitch, Kick, X/Twitter, Instagram, TikTok, Facebook) and posts alerts to Discord when they post or go live. Every server's configuration — accounts, channels, alert settings, enabled/disabled state — is fully isolated by `guild_id`.

**Platforms currently offered:** YouTube, Kick, Instagram, Twitter / X, TikTok. **On hold:** Twitch (an adapter exists under `platforms/` but isn't registered or offered in `/notify account-add`). **Not available:** Facebook (no adapter).

This is being built in phases. **Phases 1–6 are complete, Phase 7 is partially complete, and Phase 8 is partially complete.** YouTube, Twitch, Kick, X/Twitter, Instagram and TikTok are all working end-to-end: `/notify account-add` registers an account, the monitoring framework polls it, and real Discord notifications go out for new videos/Shorts (YouTube), new posts (X/Twitter, Instagram), new videos (TikTok), and live start/end (YouTube, Twitch, Kick). Facebook has **no adapter** — see the Phase 7 section below for why: Meta offers no official way to monitor a Page that hasn't individually authorized this app. Per-account alert configuration (which event types notify, embeds, role mentions) can now be viewed and changed entirely via slash commands — see the Commands section.

## Status

| Phase | What | Status |
|---|---|---|
| 1 | Project structure, config, logging, Discord client, `/notify status`, `/notify toggle`, `!sync`, Supabase connection | ✅ Done |
| 2 | Full database schema (accounts, channels, alert config, events, live state, platform health) | ✅ Done |
| 3 | Monitoring framework (scheduler, workers, rate limiting, retries, dedup, API usage manager) | ✅ Done — framework only, zero adapters registered |
| 4 | YouTube adapter, `/notify account-add\|account-remove\|account-list`, notification embeds + sender | ✅ Done |
| 5 | Twitch adapter (live/offline detection) | ✅ Done |
| 6 | Kick adapter (live/offline detection) | ✅ Done |
| 7 | X/Twitter, Instagram, Facebook, TikTok adapters | ⚠️ Partial — X/Twitter and TikTok (free, no key) and Instagram done; Facebook not built |
| 8 | `/notify alerts` (per-event-type enabled/embed toggles + viewing), `/notify account-set-mention` (role mentions), `/notify account-info` (view full config incl. custom message) | ⚠️ Partial — analytics and general admin tooling deliberately **skipped**, at the user's explicit choice (2026-09-15) |

### Platform API policy (applies to every platform integration)

Before any platform integration is built: verify that platform's *current* official API pricing, quotas, rate limits, auth/approval requirements, and available endpoints — never assume an API is free. Live monitoring should prefer official webhooks/events (e.g. Twitch EventSub) over polling wherever a platform actually offers them — the monitoring framework built in Phase 3 is polling-shaped (`PlatformScheduler` on an interval), so a webhook-based platform will need its own inbound-delivery path (see Monitoring framework below) rather than being forced through `fetch_updates()`.

- **YouTube** is polled, not pushed (WebSub needs a public HTTPS endpoint this project doesn't have yet). Since 2026-10-12 every check, live status included, costs at most 2 quota units, so it can poll as fast as every 30s — see the YouTube adapter section below.
- **Twitch (Phase 5) and Kick (Phase 6)** were deliberate polling choices too, also with sign-off where the tradeoff was real: EventSub-style webhooks are the "correct" push-based approach for both, but need a public HTTPS endpoint this project doesn't have; Twitch's EventSub WebSocket transport (which would avoid that) turned out to require a *user* access token capped far too low for monitoring arbitrary streamers, so it isn't viable at all. Polling is cheap enough on both platforms' free app-token tiers that there's no quota pressure forcing the webhook infrastructure question the way there was for YouTube.
- **X/Twitter** is free and needs no key, but X limits how often timelines can be read, so each X account is checked less often as more accounts are added (see the Twitter / X adapter section).
- **Instagram** uses Meta's official Business Discovery API: it works for any public Business/Creator account (not personal ones), needs the operator's own Meta app + Facebook Page + professional Instagram account, and needs no App Review for the operator's own use. See the Instagram adapter section below.
- **TikTok** is free and needs no key: the bot reads the public creator embed TikTok provides for showing a profile on websites. It alerts new videos only (the embed has no live status), and each TikTok account is checked less often as more accounts are added (see the TikTok adapter section).
- **Facebook (Phase 7) has no adapter.** Meta's API is built around a Page connecting *its own* account to your app, with no Instagram-Business-Discovery-style exception. The closest equivalent ("Page Public Content Access") requires Meta Business Verification and is scoped to "analyze/display" use cases with no guarantee a notification bot qualifies. Revisit if Meta's policy changes.

## Requirements

- Python 3.11+
- A Discord application/bot ([discord.com/developers/applications](https://discord.com/developers/applications)) with the **Message Content Intent** enabled
- A [Supabase](https://supabase.com) project (Postgres)

## Setup

1. **Install dependencies**

   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   pip install -r requirements-dev.txt
   ```

2. **Configure environment**

   Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` — from the Discord Developer Portal
   - `SUPABASE_URL` / `SUPABASE_KEY` — use the **secret** key (`sb_secret_...`, Dashboard → Settings → API Keys; the older `service_role` key still works but is being phased out by end of 2026). Server-side only, bypasses RLS.
   - `DEV_GUILD_ID` — optional, a guild ID for instant slash-command sync while developing
   - `DISCORD_OWNER_IDS` — optional, extra Discord user IDs allowed to run owner-only commands. The bot's own application owner/team is resolved automatically and doesn't need to be listed here.

   Every platform's credentials are optional and independent — a missing/incomplete set just disables that one platform at startup (logged, not fatal). See `.env.example` for the full list and setup links; in short:
   - `YOUTUBE_API_KEY` — a plain API key (not OAuth) from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) with the YouTube Data API v3 enabled.
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — from a [Twitch app](https://dev.twitch.tv/console/apps).
   - `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET` — from a [Kick app](https://kick.com/settings/developer).
   - X (Twitter) needs no key — it's on by default (`TWITTER_ENABLED`); see the Twitter / X adapter section.
   - `INSTAGRAM_ACCESS_TOKEN` / `INSTAGRAM_BUSINESS_ACCOUNT_ID` — a non-expiring Page token + the IG user ID of your *own* Instagram professional account. Step-by-step in "Instagram setup" below.
   - TikTok needs no key either — it's on by default (`TIKTOK_ENABLED`); see the TikTok adapter section.
   - There's no Facebook key to set — it has no adapter (see the Phase 7 section above).

   `MONITORING_*`/`YOUTUBE_*` tuning variables (polling intervals, retry/backoff, rate limits, health thresholds, quota budgets) are all optional too — sensible defaults are baked in; see `.env.example` for the full list.

3. **Run the database migrations**

   In the Supabase SQL editor, run, in order:
   - [database/migrations/0001_create_guilds.sql](database/migrations/0001_create_guilds.sql)
   - [database/migrations/0002_core_schema.sql](database/migrations/0002_core_schema.sql)
   - [database/migrations/0003_add_content_cursor.sql](database/migrations/0003_add_content_cursor.sql)
   - [database/migrations/0004_add_webhook_url.sql](database/migrations/0004_add_webhook_url.sql)
   - [database/migrations/0005_move_to_pulsenotify_schema.sql](database/migrations/0005_move_to_pulsenotify_schema.sql)
   - [database/migrations/0006_billing.sql](database/migrations/0006_billing.sql)
   - [database/migrations/0007_bot_state.sql](database/migrations/0007_bot_state.sql) — maintenance mode
   - [database/migrations/0008_website_billing.sql](database/migrations/0008_website_billing.sql) — website payments, upgrade reminders
   - [database/migrations/0009_polar_provider.sql](database/migrations/0009_polar_provider.sql) — card payments through Polar
   - [database/migrations/0010_bot_present.sql](database/migrations/0010_bot_present.sql) — which servers the bot is in (website server list)
   - [database/migrations/0011_dm_log.sql](database/migrations/0011_dm_log.sql) — DM delivery log
   - [database/migrations/0012_rename_to_disconotify.sql](database/migrations/0012_rename_to_disconotify.sql) — renames the schema to `disconotify` and tables to `notify_*`

   Then add `disconotify` under **Project Settings > Data API > Exposed schemas**. All tables live in that schema, not `public`, and the bot can't reach them until the schema is exposed.

   More migrations are added as later phases need them.

4. **Invite the bot**

   **Developer Portal → DiscoNotify → Installation:** Installation Contexts = **Guild Install** only (the bot works inside servers); Install Link = "Discord Provided Link"; Default Install Settings → Guild Install → scopes **`bot`** + **`applications.commands`** and these permissions:

   | Permission | Why |
   |---|---|
   | View Channels | See alert channels and where to post the welcome card |
   | Send Messages | Welcome card, `!` command replies, and the fallback when a webhook can't be used |
   | Manage Webhooks | Main alert delivery — through a per-channel webhook admins can rename and re-avatar |
   | Embed Links | Link previews when an alert type's embed is switched off |
   | Mention Everyone | Pinging @everyone and roles that aren't set as mentionable (without it alerts still post, the ping just doesn't notify) |

   Permissions integer **`537021440`** — invite link (Application ID from *General Information*):
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=537021440
   ```
   Not needed: Administrator, Manage Server, Read Message History, Attach Files, Manage Messages. Members' slash commands are gated by Manage Server on the member's side, not the bot's. Per-channel overrides: in each alert channel the bot needs View Channel, Send Messages and Manage Webhooks.

   **Bot → Privileged Gateway Intents → Message Content Intent: on** — needed by the owner text commands (`!sync`, `!whitelist`, `!maintenance`). Once the bot is in 100+ servers Discord asks you to apply for it during verification.

5. **Run it**

   ```bash
   python main.py
   ```

6. **Sync slash commands**

   Slash commands aren't visible in Discord until synced. In any server the bot is in (as the bot owner), run:

   - `!sync` — syncs to that server only, visible immediately (use while developing)
   - `!sync global` — syncs everywhere, can take up to an hour to propagate (use for production releases)

   (`!` is the default prefix, set by `DISCORD_COMMAND_PREFIX` in `.env`.)

   **New servers set themselves up:** when the bot joins a server it syncs its commands there automatically (so `/notify` works right away — skipped when the commands are already global, since copying global commands into a server makes Discord show each one twice), starts the free trial if billing is on, and posts a welcome card with the setup commands in the server's system channel (or the first channel it can post in). See `bot/onboarding.py`. Avoid mixing `!sync` (per-server) with `!sync global` in the same server for the same reason — duplicate commands.

## Commands

Every slash command lives under one top-level `/notify` group (`commands/notify_group.py`) — there is no bare `/status` or `/toggle`. Account management (`account-add`/`account-remove`/`account-list`) is flattened into direct subcommands of the same group rather than nested under its own `account` subgroup: Discord doesn't allow mixing plain subcommands and subcommand groups as siblings under one parent, and `/notify toggle` (a plain subcommand) was the pattern to match.

| Command | Description | Permission |
|---|---|---|
| `/notify status` | This server only: whether monitoring is on, a **Subscription** section (state, card/crypto, renewal or end date, how to manage it) when billing is on, and for every monitored account — the account (linked), the channel its alerts post to, how many alert types are on, who gets pinged, whether it's live, when it was last checked and when its last alert went out, plus each platform's check interval. Works while the server is paused | Anyone |
| `/notify global status` | Bot-wide health: uptime, latency, server count, database connection, per-platform monitoring health and request counts. Private reply, hidden from `/notify help` for everyone else | Bot owner only (`DISCORD_OWNER_IDS` or the Discord application's owner) |
| `/notify subscribe` | Shows this server's trial/subscription state and gives a signed **Subscribe on the website** link for this server (the website creates the Polar checkout; during the trial nothing is charged until it ends). The bot never sends a direct Polar checkout link. Says "free" while billing is off | Manage Server |
| `/notify cancel` | Explains what cancelling means for this server (trial with a saved card → never charged; paid card → active until the end of the paid month; crypto → nothing renews) and opens Polar's billing portal (cancel or change card) — **only for the person who subscribed or the server owner** | Manage Server |
| `!whitelist add [server_id] [note]` · `!whitelist remove [server_id]` · `!whitelist list` | Let a server use everything without paying, undo that, or list whitelisted servers. `server_id` defaults to the current server. A text command on purpose: Discord can't hide a slash command from individual users, so this never appears in anyone's command menu or `/notify help`, and anyone else typing it gets no reply | Bot owner only |
| `!maintenance on [message]` · `!maintenance off` · `!maintenance` | Pause DiscoNotify in **every** server — all commands (except the owner's) and all monitoring — with your message shown to members and "🛠️ Under maintenance" as the bot's status. Asks to confirm first. Survives restarts (stored in the database, migration 0007), so: switch on → update → restart → check → switch off. Routine updates don't need it: a restart is seconds and nothing is lost (content cursors and live states are saved) | Bot owner only |
| `/notify help` | Lists every command and what it does — built by introspecting the command group itself, so it can't drift out of sync with what's actually registered | Anyone |
| `/notify toggle state:<on\|off>` | Enable/disable DiscoNotify for this server. Off pauses monitoring (its accounts aren't polled and no alerts go out — before 2026-10-13 it only blocked commands) and blocks other commands; `toggle`, `status`, `help`, `subscribe` and `cancel` still work | Manage Server |
| `/notify account-add platform` | Start monitoring a YouTube/Kick/Instagram account. Opens a form: the account (handle/URL/ID), a channel picker for where alerts post, and an optional role to mention. Refuses a channel the bot can't post in. Adding an account the server already has — however it's typed (`@NASA`, a profile link, a channel ID) — says "You've already added …" and where its alerts go, without calling the platform | Manage Server |
| `/notify account-remove account` | Stop monitoring an account (autocompletes from this server's configured accounts). Asks for confirmation first — Remove/Cancel buttons only the person who ran it can press, expiring after 60s with nothing deleted | Manage Server |
| `/notify account-list` | List accounts monitored in this server, with status | Anyone |
| `/notify account-set-message account` | Opens a form with a multi-line message box (pre-filled with the current message) and which alert type it applies to (all, or one). Leave the box empty to go back to the default message; see below | Manage Server |
| `/notify account-set-mention account` | Opens a form: who to mention (a role, @everyone, or nobody), a role picker, and which alert type it applies to — pre-filled with the current setting | Manage Server |
| `/notify alerts account` | Opens a form with two multi-select lists covering every alert type the account has: which ones send alerts, and which ones show the embed/link preview — pre-filled with the current settings | Manage Server |
| `/notify account-info account` | Show an account's full alert configuration — every event type's enabled/embed/mention state, plus its custom message if one is set | Anyone |
| `!sync [global]` | Sync slash commands to this server, or globally. A text command, not a slash command — if the command tree is out of sync, `/notify` itself might not be registered yet, so syncing can't depend on it. | Bot owner only |

Per-account alert preferences (which event types notify, mentions, embed on/off) are seeded with sensible defaults when an account is added — new posts/videos/Shorts/live-start on, stream-ended off, matching the spec's own example. All of them are viewable and changeable via slash commands (Phase 8). Since 2026-10-12, commands that need more than one choice open a modal form (`commands/accounts/modals.py`) pre-filled with the current settings, rather than taking everything as slash-command options; the slash command itself only picks the platform or the account (an account dropdown inside a form is capped at 25 options, so accounts stay on autocomplete). `/notify alerts` turns every event type's notifications and embeds on or off in one form, `/notify account-set-mention` sets or clears who is pinged for an account (Discord's role picker doesn't offer @everyone, so it's a separate choice in the form), and `/notify account-info` shows everything for an account at a glance — including the custom message, which previously could only be *set* (`account-set-message`), never viewed back.

Deliberately **not** built in Phase 8, at the user's explicit choice: analytics (event-count/history stats) and general admin tooling (e.g. wiring up `notify_audit_logs`, which still exists but nothing writes to it). Revisit if wanted later.

### Custom notification messages

`/notify account-set-message` lets an admin replace the default message text with their own template — typed into a multi-line box in its form, which a slash-command option can't do — e.g.:

```
Hey @everyone, {creator} just posted a new post! Go check it out!
{url}
```

Placeholders (`notifications/templates.py`): `{creator}` `{title}` `{description}` `{url}` `{platform}` `{category}` `{viewers}` `{mention}` (the account's configured mention role, if any — empty string otherwise). An unrecognized `{placeholder}` is left as literal text rather than erroring, and the rendered result is truncated to Discord's 2000-character message limit if substitution pushes it over — a malformed template can never crash delivery.

Two different trust levels are handled on purpose: the **template** is admin-authored (the command requires Manage Server) and may contain literal `@everyone`, `@here`, or role mentions — that's explicit configuration, matching the example above. The **values substituted into it** (video title, post description, ...) come from external platform content and are always mention-escaped first (`discord.utils.escape_mentions`), so a stream title containing the literal text "@everyone" can never inject a mention the admin didn't type themselves. `AllowedMentions.everyone` is only set `True` when the *rendered* content actually contains a literal `@everyone`/`@here` — which, given the escaping above, can only happen if the admin put it in the template.

By default a custom message replaces the text line above the alert card; the card itself still follows the account's `embed_enabled` setting (see below).

### Messages are Components V2 cards

Every message the bot sends — alerts and command replies alike — is a Components V2 layout (`discord.ui.LayoutView`), never plain message content or a legacy embed (since 2026-10-12).

- **Alerts** (`notifications/cards.py`): a text line on top (the headline, e.g. "🔴 **Vector Games** is live now!", with the mention role in front — or the admin's custom message), then an accent-coloured container with the channel name + avatar, the linked title, a "… is now live on YouTube!" line, the description (trimmed to 350 characters), the full-size thumbnail, a "Watch on YouTube" link button, and a small footer with the platform and time (rendered in each viewer's timezone). YouTube, Kick and Instagram each have their own colour and labels; pieces the platform didn't provide are left out. Turning `embed_enabled` off for an alert type (`/notify alerts`) sends just the text line with the link.
- **Command replies** (`ui/cards.py`): one card helper used for every confirmation, list, status and error, so they look consistent. Colours come from the logo: **white** for normal replies and confirmations, **logo red `#E2364A`** for errors and anything needing attention (`config/constants.py`). Alert cards keep their platform's colour. Replies never ping by default — a custom message preview or account-info showing "@everyone" displays it without triggering it.
- **Mention safety:** unlike legacy embeds, text inside a V2 message *does* ping. Only the alert's top line is meant to carry a mention; every piece of platform-sourced text (titles, descriptions, channel names) is mention-escaped before it goes anywhere in the message, so a video titled "@everyone" can't ping a server.
- A V2 message gets no automatic link preview from Discord, which is why every platform's alert is built as a card from the event's own data.

## Payments setup

Per-server subscriptions (**`BILLING_PRICE_USD`** a month, $1.99 by default) live in `billing/` with the commands in `commands/admin/billing.py`. **Nothing is enforced until `BILLING_ENABLED=true`** — until then every server keeps full access and no trial clock starts.

**How it works**
- **Card — Polar ([polar.sh](https://polar.sh)).** Polar is a merchant of record: it charges the customer, handles sales tax and pays you out. `/notify cancel` opens Polar's customer portal, where the subscription is cancelled or the card changed. Two ways to subscribe:
  - **Website mode** (`BILLING_SUBSCRIBE_URL` **and** `BILLING_LINK_SECRET` set): `/notify subscribe` gives a button to your website with a **signed, 30-minute link** (server, who asked, trial days left). The website creates the Polar checkout, the customer pays, and the website shows a **success / warning / error** alert. Crypto can live on the same page. Spec and reference code: [docs/website-integration.md](docs/website-integration.md). People can also open the site directly, **log in with Discord and pick their server from a list** (only servers they manage that the bot is in — tracked by `notify_guilds.bot_present`, migration 0010).
  - Without `BILLING_LINK_SECRET` there is no card subscribe link at all (the bot no longer creates direct Polar checkouts); only the crypto page, if `BILLING_SUBSCRIBE_URL` is set.
  Either way the server ID, the person and the channel travel as metadata, and the bot finds the payment by itself.
- **Crypto — NOWPayments, paid on your website** (prepaid 3/6/12 months). Optional; in bot mode the button shows when `BILLING_SUBSCRIBE_URL` is set.
- **Trial:** a server's 7-day free trial starts the first time the bot sees it with billing on, and removing and re-adding the bot doesn't restart it. Subscribing by card during the trial saves the card and charges nothing until the trial ends (the bot sets each checkout's trial to the days left; the Polar product has no trial of its own). Cancel during the trial → never charged; after paying → the server stays active until the end of the paid month.
- **Confirmation:** every 60s the bot lists Polar's subscriptions (and checks NOWPayments) itself — a payment only counts once the provider says so — then posts "✅ Payment received" in the channel `/notify subscribe` was used in. Known subscriptions are refreshed every 10 minutes (renewals, cancellations, refunds). No webhook endpoint is needed, so there's no inbound attack surface.
- **Upgrade reminders:** when the trial or a subscription runs out, the bot DMs the **server owner** a reminder with subscribe buttons — right away, then every `BILLING_REMINDER_EVERY_DAYS` (7), at most `BILLING_REMINDER_MAX` (3) times. If DMs are closed it posts in the server's system channel. Paying resets the count. Whitelisted servers never get one. Any other command from an unpaid server answers "🔒 Subscription needed".
- **Grace:** 3 days after paid time runs out, then monitoring pauses. Settings are never deleted; monitoring resumes the moment the server pays.
- **Price:** `BILLING_PRICE_USD` is the price shown in Discord and used for crypto amounts — keep it equal to the Polar product's price (the startup log warns if they differ). Polar keeps existing subscribers on the price they signed up at.

**Security rules built in**
- Only people with **Manage Server** can run `subscribe` / `cancel`, with a cooldown (3 uses per 30 s).
- Website links are **HMAC-signed and expire** (`BILLING_LINK_TTL_MINUTES`, 30 by default), so nobody can forge a server ID, and the website needs no Discord login.
- The **billing portal link** is pre-authenticated as the paying customer (it shows their email and card), so only **the person who subscribed or the server owner** can open it; other admins are told who can.
- Checkout and portal links from Polar's API are only used when they're **https on polar.sh**; replies are ephemeral (only the person who asked sees them); a checkout link is reused for 10 minutes instead of calling Polar again.
- A subscription that ends **early** (refund, chargeback, revoked) cuts access at that moment — without touching prepaid crypto time.
- **Members never see technical text** — anywhere. Errors from a platform, Polar or the database are replaced by a plain-English sentence ("X isn't responding right now, please try again in a few minutes"), with a short **reference code** when the cause isn't something they can fix; the full detail goes to the log under that same code, so you can search the logs for it. `/notify status` shows "couldn't be checked just now — retrying automatically" for a broken account, never the raw error.
- The access token only needs the scopes below, lives only in the environment (never in git, never logged), and the bot has no webhook endpoint.

**1. Database:** run [0006_billing.sql](database/migrations/0006_billing.sql), [0008_website_billing.sql](database/migrations/0008_website_billing.sql) and [0009_polar_provider.sql](database/migrations/0009_polar_provider.sql) and [0010_bot_present.sql](database/migrations/0010_bot_present.sql) in the Supabase SQL editor.
   - [database/migrations/0011_dm_log.sql](database/migrations/0011_dm_log.sql) — DM delivery log
   - [database/migrations/0012_rename_to_disconotify.sql](database/migrations/0012_rename_to_disconotify.sql) — renames the schema to `disconotify` and tables to `notify_*`

**2. Polar (cards)**
1. Create an organization at polar.sh and finish the **Account Review** (product, identity, payout account via Stripe Connect, website, support email).
2. **Products → New product:** *Subscription*, **monthly**, fixed price equal to `BILLING_PRICE_USD`, **no free trial** on the product (the bot adds the trial per checkout), no benefits. Copy its ID → `POLAR_PRODUCT_ID`.
3. **Settings → Developers → New access token** (an *organization* access token) → `POLAR_ACCESS_TOKEN`. Give it only: `checkouts:write`, `subscriptions:read`, `customer_sessions:write` (and optionally `products:read`, so the startup check can compare the price). Set an expiry and note it in your calendar. Put it **only** in Railway's variables.
4. Optional: `POLAR_SUCCESS_URL` (where Polar sends people after paying; otherwise Polar's own page). `POLAR_SANDBOX=true` uses sandbox.polar.sh (a separate account and token).
5. In Polar's settings turn on the customer email notifications (receipts, trial-ending reminders) — the bot doesn't send those.

**3. NOWPayments (crypto, optional)**
1. Sign up at nowpayments.io and add your **payout wallet(s)**.
2. **Settings → Payments → API keys** → `NOWPAYMENTS_API_KEY` (the bot only reads payments with it). Use a **live** key with `NOWPAYMENTS_SANDBOX=false`.
3. Your website creates the payment (with `order_id` = `"<guild_id>-<anything unique>"`) and inserts a `pending` row into `disconotify.notify_payments`: `guild_id`, `provider = 'nowpayments'`, `external_id` = NOWPayments' `payment_id`, `months` (3/6/12), `amount_usd`, `status = 'pending'`. The bot checks it with NOWPayments and credits the months once paid — but refuses it if NOWPayments' `order_id` names another server or its USD price is lower than `amount_usd`. Inserting needs the Supabase service-role key on the website's **server side** only.
4. Each coin has a **minimum payment** that moves with network fees (2026-10-13: under $1 for USDT-BNB/LTC/SOL/ETH, ~$11.18 for USDT on TRON, ~$21.98 for BTC) — the website should check `/min-amount` before offering a plan.

**Sandbox vs live:** Polar's sandbox (sandbox.polar.sh) and live are separate worlds — separate account, token, product ID and customers. Use the same one everywhere (the bot's `POLAR_*` variables on Railway **and** the website). A subscription made in the sandbox doesn't exist in live Polar, so a live bot can't open its billing page ("We couldn't open the billing page…"); when you move from testing to live, clear the test server's card fields (`card_subscription_id`, `card_customer_id`, `card_status`, `card_cancels_at` in `notify_billing`).

**4. Turn it on:** set `POLAR_ACCESS_TOKEN`, `POLAR_PRODUCT_ID` (for website mode also `BILLING_SUBSCRIBE_URL` and `BILLING_LINK_SECRET` — a long random string shared with the website, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`; otherwise `BILLING_SUBSCRIBE_URL` only if you offer crypto), `BILLING_ENABLED=true`, redeploy, `!sync`. Whitelist your own servers first with `!whitelist add` (in each server, or `!whitelist add <server_id>`). The startup log shows "Billing enabled (card: on, …)" and warns about a missing token or a price mismatch.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use an in-memory fake for the Supabase query builder ([tests/unit/fakes.py](tests/unit/fakes.py)) — no live Supabase project or Discord connection is needed to run them. `tests/integration/` runs the same way (still no live services) but exercises multiple real components together with real (short) timers, e.g. a fake adapter registered with the real `MonitoringManager`, polled by a real background scheduler loop, delivered through the real dedup path.

## Architecture

```
main.py              entry point: load config, set up logging, run the bot
bot/
  client.py           DiscoNotifyBot (discord.ext.commands.Bot subclass), command tree gating, owns MonitoringManager
  events.py            Discord event listeners + centralized error handling
  lifecycle.py          process run/shutdown loop (signal handling, graceful close)
commands/
  notify_group.py   the single shared `/notify` app_commands.Group every subcommand below attaches to
  admin/                status.py, toggle.py, help.py — decorate against the shared group; sync.py — a real Cog (text command, not a slash command)
  accounts/              account_commands.py — account-add/-remove/-list/-set-message/-set-mention/-info, alerts; flattened into the shared group; see its docstring for why
platforms/
  base.py              PlatformAdapter interface every platform implements; event_types_for_capabilities() helper
  youtube/               the first concrete adapter — client.py (HTTP), parser.py (pure JSON->shapes), adapter.py (ties them together + quota budgeting)
  twitch/                 live/offline only — app-token polling, no adapter-level quota budgeting needed (see Twitch adapter section)
  kick/                   live/offline only — same shape as twitch/, one endpoint does both identifier resolution and live status
  twitter/                new posts (no key needed) — see Twitter / X adapter section
  tiktok/                 new videos (no key needed) — see TikTok adapter section
  instagram/              new posts + Reels via Business Discovery — see the Instagram adapter section and its setup steps
monitoring/
  manager.py            single start()/stop() entry point; owns one PlatformScheduler per registered adapter
  scheduler.py           polls one platform's enabled accounts on an interval, shared rate limiter
  worker.py               polls one account: fetch/normalize, live-transition detection, error isolation
  rate_limiter.py         sliding-window async limiter, shared per-platform, cost-weighted (see YouTube section)
  retry.py                 exponential backoff; skips retrying permanent/rate-limit errors
  health.py                per-platform health/usage tracker (batches request counts, gates on rate limits)
events/
  models.py             NormalizedEvent, EventType — the common event shape every adapter produces
  processor.py            queue + consumer: dedup against `notify_events`, then hands new events to a sink
ui/
  cards.py               the shared Components V2 card every command reply uses (never pings by default)
notifications/
  cards.py               NormalizedEvent -> Components V2 alert layout (text line + card); see "Messages are Components V2 cards"
  templates.py             safe {placeholder} substitution for admin-authored custom messages, with mention-escaping on substituted values
  sender.py                the real EventProcessor sink: alert-config gating, mention scoping, marks a channel invalid instead of retrying forever
database/
  client.py            owns the Supabase AsyncClient connection lifecycle
  models.py             TypedDict row shapes, one per table — no behavior
  repositories/         guild-scoped data access, one file per table
  migrations/           raw SQL, run manually in Supabase for now
config/
  settings.py           pydantic-settings — env vars in, validated Settings out
  constants.py           small app-wide constants (bot name, embed colors)
utils/
  logger.py             logging setup
  errors.py              DiscoNotify's exception hierarchy (PlatformError, RateLimitedError, ...)
tests/
  unit/                  fast tests, no live external services
  integration/            multiple real components wired together, still no live external services
```

## Database schema

Three migrations exist so far:

- **`0001_create_guilds.sql`** — `notify_guilds`: one row per server, tracking only its enabled/disabled state (Phase 1).
- **`0002_core_schema.sql`** — everything needed to configure and run monitoring:
  - `notify_bot_settings` — guild-wide preferences (1:1 with `notify_guilds`); not used by any command yet
  - `notify_platform_accounts` — one row per monitored account per guild; duplicate (guild, platform, account) prevented by a unique constraint; created by `/notify account-add`
  - `notify_notification_channels` — Discord channels registered to receive alerts; marked `is_valid = false` rather than deleted if the bot loses access
  - `notify_account_channels` — routes one account's alerts to one channel (many-to-many)
  - `notify_alert_configurations` — per-account, per-event-type toggles (new post, live started, mentions, embeds, ...); seeded with defaults by `/notify account-add`
  - `notify_events` — the deduplication ledger: `(account_id, event_type, platform_event_id)` is unique, checked before ever sending a notification
  - `notify_live_states` — current live/offline status per account, so a bot restart mid-stream doesn't re-announce it
  - `notify_audit_logs` — append-only record of administrative actions; not written by anything yet
  - `notify_platform_health` — **not** guild-scoped; one row per platform (YouTube, Twitch, ...) tracking the bot's own API usage/quota/rate-limit state against that provider — created on first use
- **`0003_add_content_cursor.sql`** — adds `notify_platform_accounts.content_cursor`, the persisted "newest content seen" pointer. Found and fixed during Phase 4: without persisting it, a bot restart reset an account's cursor to NULL, which would either re-flood the channel's entire back-catalog or silently skip whatever was published during the downtime, depending on how that case was handled. See `monitoring/worker.py`'s docstring.
- **`0004_add_webhook_url.sql`** — adds `notify_notification_channels.webhook_url`, so the per-channel Discord webhook notifications are delivered through (see Monitoring framework below) is created once and reused across restarts instead of a new one appearing every time the bot starts up.
- **`0005_move_to_pulsenotify_schema.sql`** — moves every table, domain and the `updated_at` trigger function out of `public` into a dedicated `disconotify` schema, and grants `service_role` access to it. The bot's client is pinned to that schema (`DB_SCHEMA` in `database/client.py`), and the schema has to be listed under the Data API's exposed schemas.

`platform`, `event_type`, and `account_status` are Postgres domains (not raw `text`), so the valid-value list for each lives in exactly one `ALTER DOMAIN`-able place instead of being repeated across every column that uses it.

## Monitoring framework

The pipeline (section 9 of the original spec):

```
MonitoringManager
   └── one PlatformScheduler per registered adapter
          └── one AccountWorker per enabled account of that platform
                 poll: fetch_updates() / get_live_status() → normalize → submit
                            ↓
                     EventProcessor (single shared queue + consumer)
                            ↓
                  dedup against `notify_events` (unique on account+type+platform_event_id)
                            ↓
                   new? → notification sink (NotificationSender, see below)
```

What's real and tested:

- **`PlatformAdapter`** ([platforms/base.py](platforms/base.py)) — the interface a concrete adapter implements: `validate_account`, `fetch_updates`, `get_live_status`, `normalize_event`, plus a `capabilities` set (`POSTS`, `VIDEOS`, `SHORTS`, `STREAMS`, `LIVE_STATUS`, `CHANNEL_UPDATES`) so the worker only calls what a platform actually supports.
- **Rate limiting** — one `RateLimiter` (sliding window) per platform, shared by every account worker for that platform, since the limit is against the platform's API as a whole. It's cost-weighted (`acquire(cost=N)`/`try_acquire(cost=N)`), not just a request count — added specifically because YouTube's API charges wildly different costs per endpoint (see below).
- **Retries** — exponential backoff with jitter; a `PermanentPlatformError` (account gone) or `RateLimitedError` (429/quota) is never retried within a call — see below.
- **The 429 / quota-exhaustion path** — an adapter raises `RateLimitedError(message, retry_after=...)`; the worker routes that to `PlatformHealthTracker.record_rate_limited()` instead of counting it against the account, and `PlatformScheduler` checks `is_rate_limited()` before every poll cycle and skips that platform entirely until the window passes. One platform hitting its quota never touches any other platform's scheduler.
- **API usage tracking** — `PlatformHealthTracker` batches request counts in memory and flushes to the (not guild-scoped) `notify_platform_health` table once per scheduler tick; surfaced in `/notify status`.
- **Live-transition detection** — the worker, not the adapter, compares a `LiveStatus` snapshot against the persisted `notify_live_states` row to decide whether to fire `STREAM_STARTED`/`STREAM_ENDED` — this is what makes "don't re-announce a stream that was already live before a restart" (section 8) work for every platform for free, rather than every adapter reimplementing it.
- **Deduplication** — `EventProcessor` is the one place that checks `notify_events` and decides "new" vs "already seen," so that guarantee holds regardless of which worker or platform produced the event.
- **Notification delivery** — `EventProcessor`'s sink is `NotificationSender.handle` ([notifications/sender.py](notifications/sender.py)): checks the account's alert config for that event type, builds the Components V2 alert layout (see "Messages are Components V2 cards" above), scopes mentions to just the configured role, or @everyone when that's the configured mention (`AllowedMentions`, never arbitrary users — section 36), and sends to every channel linked to that account. A channel Discord says is gone (`Forbidden`/`NotFound`) gets marked `is_valid=false` instead of retried forever (section 23); other Discord errors are logged and skipped, not treated as fatal.
- **Delivery is via a per-channel webhook, not the bot's own user.** `/notify account-add` creates the channel's webhook right away (not waiting for the first alert), specifically so the admin can rename it / give it a custom avatar in Discord's own channel-integrations UI before anything is ever sent through it — and, as of 2026-09-18, every send actually respects that customization: `webhook.send()` never passes a per-message `username`/`avatar_url` override, so the webhook keeps displaying whatever name/avatar it's actually configured with (the "DiscoNotify" default from creation, or the admin's own rebrand) instead of being silently stomped back to the raw platform account name on every single notification — a real bug (confirmed via a live screenshot showing an admin-customized webhook reverting to the raw `@handle` name) fixed the same day. Its URL is persisted on the channel row (`webhook_url`, migration 0004) and reused after that — never a new webhook per message, and never a second one for a channel that already has one, even across multiple accounts/multiple sends. Before trusting a stored URL, `NotificationSender.get_or_create_webhook()` actually verifies with Discord that the webhook still exists (`webhook.fetch()`) — found necessary in practice: a webhook can be deleted out from under the bot by a server admin or some unrelated automation/integration, and without this check a stale URL would just get handed back forever, silently, with nothing ever getting (re)created. If the bot lacks "Manage Webhooks" in a channel, or webhook delivery fails outright, it falls back to sending as the bot itself rather than dropping the alert.

**Known architectural gap:** this framework is polling-shaped — `PlatformScheduler` calls `fetch_updates()`/`get_live_status()` on an interval. The platform-API policy above prefers webhooks (Twitch EventSub, YouTube WebSub) where a platform offers them and the cost of polling is too high; a webhook-based platform will need its own inbound HTTP receiver feeding events into the same `EventProcessor.submit()`, bypassing the polling scheduler rather than being forced through it. Not designed yet.

## YouTube adapter (platforms/youtube/)

The first concrete `PlatformAdapter`. API availability was verified against Google's current documentation before writing any code (see `platforms/youtube/adapter.py`'s module docstring for the full table) — summary:

| Feature | Method | Cost per check | Notes |
|---|---|---|---|
| New videos | `playlistItems.list` on the uploads playlist | 1 unit | Reliable, cheap |
| Shorts | duration ≤ 3 min **and** vertical/square embed (`videos.list part=player`) | (included) | **No official "is Short" field exists.** Falls back to duration ≤ 60s if the embed size is missing. `NormalizedEvent.metadata["is_short_heuristic"]` marks it as a guess |
| Premieres | uploads playlist + `videos.list` | (included) | Announced as a new video/Short when the Premiere starts — not as a live stream |
| Community posts | — | — | **Not implemented — no public API endpoint exists for this at all** |
| Live start/end | uploads playlist + `videos.list` | 1 unit | See "Live detection" below |
| Upcoming/scheduled streams | — | — | Tracked internally so the moment they go live is caught, but not announced |

Credentials: a plain API key, no OAuth (everything above is public data).

**Live detection (2026-10-12):** a broadcast appears at the top of the channel's uploads playlist as soon as it's scheduled or goes live — verified against the real API: every currently-live stream sampled was at position 0 (one had started two minutes earlier), scheduled ones at positions 0–2. So the same 1-unit `playlistItems.list` that finds new uploads also finds new broadcasts, and one batched `videos.list` refreshes the state (upcoming → live → ended) of every broadcast the adapter is tracking, even after it scrolls off the first page. Real streams report a duration of `P0D` while upcoming/live; a Premiere carries its real video length, which is how the two are told apart. This replaced `search.list(eventType=live)` (100 units/call), whose cost had forced YouTube's whole scheduler — new-video detection included — down to one check every ~18 minutes, the main source of notification delay.

**Quota and poll interval:** the free tier is 10,000 units/day, shared by every YouTube channel this bot monitors — one budget per API key, not per account or guild. A channel check costs at most 2 units, so `YouTubeAdapter.poll_interval_seconds()` stretches the interval with the number of *distinct* channels to fit 90% of the daily quota (the rest is headroom): 30s for 1 channel, ~58s for 3, ~96s for 5, ~192s for 10. The scheduler re-asks every cycle, so adding/removing accounts adjusts it without a restart.

- `YOUTUBE_DAILY_QUOTA_UNITS` (default 10,000) sizes that math and the adapter's internal `RateLimiter`. Raise it only after Google approves a quota extension — you cannot pay to increase it directly.
- `YOUTUBE_MIN_POLL_INTERVAL_SECONDS` (default 30) is the floor.
- Every quota spend uses `try_acquire()` (non-blocking): if the day's budget is ever spent, that check is skipped — `get_live_status()` returns `None` ("unknown"), never "offline", so a running stream is never falsely announced as ended.
- Two guilds monitoring the same creator share one check per tick (`_ChannelSnapshot`, keyed by channel ID), so the cost is per channel, not per guild-scoped account row.
- A real 429 or `quotaExceeded` response additionally pauses the platform via the framework's rate-limit path described above.

**Cursor:** stored as `"<videoId>|<time added to playlist>"`. If the cursor's video disappears from the page (deleted or made private), only uploads added after that timestamp are reported — previously that case re-announced the whole first page. Older bare-ID cursors still work, and are rewritten in the new format on the next poll.

## Twitch adapter (platforms/twitch/)

Live/offline detection only (`Capability.LIVE_STATUS`) — see `platforms/twitch/adapter.py`'s module docstring for the full research writeup. Summary:

| Feature | Method | Auth | Notes |
|---|---|---|---|
| Live/offline | `GET /helix/streams?user_id=` | app access token | No broadcaster authorization needed — this event requires no scope. |
| Channel resolution | `GET /helix/users?login=` | app access token | Resolves a login/URL to a stable numeric user ID. |

Credentials: `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`, an app access token via OAuth client-credentials — no per-broadcaster authorization, no OAuth redirect flow. Twitch's app-token bucket is 800 points/minute and this adapter spends 1 point/account/poll, so unlike YouTube no adapter-level quota budgeting was needed — the framework's own generic per-platform `RateLimiter` (`MONITORING_RATE_LIMIT_REQUESTS`/`_PERIOD_SECONDS`, default 30/60s) is already far more conservative.

**Why polling instead of EventSub, and why that needed sign-off:** EventSub's WebSocket transport would avoid needing a public HTTPS endpoint, but requires a *user* access token capped at a total subscription cost of 10 per (client_id, user) — nowhere near enough for monitoring arbitrary streamers who haven't authorized this app. EventSub's webhook transport works fine with an app token and a 10,000-cost ceiling, but needs a publicly reachable HTTPS callback (valid TLS, port 443) this project doesn't have. Given the choice between (a) polling now or (b) building that public endpoint first, **the user chose (a)** — Twitch's cost/scale here makes polling cheap enough that there's no pressure to build the webhook infrastructure.

## Kick adapter (platforms/kick/)

Live start/end detection via Kick's official Public API (docs.kick.com), re-verified against the live API on 2026-10-12 — see `platforms/kick/adapter.py`'s module docstring.

| Feature | Method | Auth | Notes |
|---|---|---|---|
| Resolve a channel name | `GET /public/v1/channels?slug=` | app access token | An unknown name comes back as HTTP 400 "Invalid request"; `account-add` reports "no Kick channel named …" |
| Live/offline | `GET /public/v1/livestreams?broadcaster_user_id=…` (up to 50 per call) | app access token | Only live broadcasters are returned, with title, thumbnail, start time, category, viewer count and profile picture |

**One batched call per check for every Kick account**, however many are monitored: the first account polled in a tick fetches all known broadcasters at once and the rest are served from that result. Kick documents no rate limit and sends no rate-limit headers, so the cadence is a fixed 30s (`KICK_POLL_INTERVAL_SECONDS`).

Credentials: `KICK_CLIENT_ID`/`KICK_CLIENT_SECRET`, an app access token via OAuth client-credentials (valid 60 days, refreshed automatically) — described by Kick's docs as usable "when user login is not required."

**Kick exposes no per-session stream ID.** `platforms/kick/parser.py` builds one from the broadcaster and the stream's start time — stable for the whole session, different for the next. Without it, every session of a channel shared one dedup key and only the first "stream ended" alert ever went out (fixed 2026-10-12; `monitoring/worker.py` also falls back to the session's start time for any platform without stream IDs).

## Twitter / X adapter (platforms/twitter/)

Alerts for new **original posts** (quote posts included; retweets and replies left out) from any public X account. **No API key and no cost** — the bot reads the account's public embedded timeline (the feed X provides for embedding a profile on websites), which returns its latest ~20 posts with text, photos/video previews, name and avatar.

**Check speed:** X allows 30 timeline requests per 15 minutes per IP address, counted **separately** for lookups by @handle and by user ID. Adding an account looks it up by @handle; routine checks read it by user ID — so checks never use up what adding needs. Each X account is fetched once per check no matter how many servers follow it, and the interval stretches with the number of different X accounts to use about 20 of those 30 (a request sometimes counts twice):

| Different X accounts | Checked every |
|---|---|
| 1 | 60 seconds (`TWITTER_MIN_POLL_INTERVAL_SECONDS`) |
| 10 | 7.5 minutes |
| 50 | 37.5 minutes |

When X reports a window is used up, that kind of request waits for the reset (other platforms aren't affected). Adding an account while X is busy says "try again in about N minutes" and adds nothing. On a host whose IP address is shared with other apps (e.g. Railway), others' requests count too — a static outbound IP gives the bot the full limit.

**Notes**
- Added by @handle or profile URL. The handle is turned into the account's ID with FxTwitter's free public API (api.fxtwitter.com — the service behind fxtwitter.com link previews), then read by ID, so adding works even when other apps on a shared host IP have used up X's by-@handle limit; if FxTwitter is down, X is asked by @handle instead. Private, suspended and never-posted accounts can't be added.
- A newly added account's existing posts are never alerted; an account with no posts gets its first one alerted.
- Checks go by the account's user ID, so an account that changes its @handle keeps working (alerts link to the new handle).
- `TWITTER_ENABLED=false` turns X off.

## TikTok adapter (platforms/tiktok/)

Alerts for new **videos** from any public TikTok account. **No API key and no cost** — the bot reads the account's public creator embed (`tiktok.com/embed/@name`, the page TikTok provides for showing a profile on websites), which returns the account's ID, name, avatar and its latest ~10 videos with caption and cover image. There is no live-stream alert: the embed doesn't show live status.

**Check speed:** TikTok publishes no request limit for the embed, so the bot is deliberately gentle: one request per account per check (shared by every server that follows the account), at most one request every 1.5 seconds, and an interval of 4 seconds × the number of different TikTok accounts:

| Different TikTok accounts | Checked every |
|---|---|
| 1–30 | 2 minutes (`TIKTOK_MIN_POLL_INTERVAL_SECONDS`) |
| 100 | ~6.7 minutes |
| 300 | 20 minutes |

**Errors**
- If TikTok blocks the bot's requests (HTTP 403/429/503 or a bot-check page), all TikTok checks pause for 10 minutes (or the time TikTok asks for); other platforms aren't affected. Adding an account then says "TikTok is busy right now, try again in about N minutes".
- Timeouts and server errors are retried automatically with the usual backoff; the account shows as "couldn't be checked just now — retrying" in `/notify status` and recovers by itself.
- An account TikTok stops showing is only marked invalid after it has been missing in three separate checks at least five minutes apart, so one bad answer can't switch off a working account.

**Notes**
- Added by @username or profile URL (a video link works too; short `vm.tiktok.com` links can't be resolved and say so). Private accounts and unknown usernames can't be added.
- A newly added account's existing videos are never alerted; an account with no videos gets its first one alerted.
- The account is stored by its numeric ID, but TikTok's embed is looked up by username: an account that **changes its username** shows as missing, is marked invalid after the checks above, and has to be re-added.
- Check speed and errors are visible in `/notify status` like every other platform.
- `TIKTOK_ENABLED=false` turns TikTok off.

## Instagram adapter (platforms/instagram/)

New posts and Reels from any public Instagram **Business or Creator** account, via Meta's official **Business Discovery** API — no authorization from the monitored account needed. Verified against the live Graph API (v26.0) on 2026-10-12.

| Feature | Method | Cost | Notes |
|---|---|---|---|
| New posts | `GET /{your_ig_user_id}?fields=business_discovery.username(...)` | 1 call per account check | Caption, image, permalink, timestamp |
| Reels | same call | (included) | Told apart by `media_product_type` = `REELS`; announced as "posted a new Reel" (still the **New Post** alert type) |
| Stories, live | — | — | Not exposed for other accounts by any official API |

Constraints:
1. **Personal accounts can't be monitored** — only Business/Creator accounts. Meta answers "Cannot find user" for anything else, and `account-add` says so.
2. **Lookup is by username only.** If a monitored account renames its handle, Meta answers "Cannot find user", the account is marked invalid, and it has to be re-added.
3. **Rate limit: 200 calls per hour in total** while the Meta app is in Development mode (measured via the `x-app-usage` header). Each account costs one call per check, so the interval stretches with the number of distinct accounts — 30s for 1, ~120s for 5, ~240s for 10 — and doubles/quadruples when Meta reports ≥70%/≥90% of the hour used. Two Discord servers watching the same account share one lookup. On top of that the cadence is **adaptive per account** (2026-10-14): an account whose newest post is under an hour old is checked at that full speed, 1–6 hours old every ~2 minutes, older (or no posts) every ~4 minutes — and it's back to full speed the moment a new post shows up. Measured on two real accounts: ~30 lookups/hour instead of ~150. Each lookup fetches only the 5 newest posts (Meta also counts processing time). Accounts with every alert type switched off aren't checked at all, on any platform.
4. **No App Review needed for your own use** — with the operator holding a role on the Meta app, Development mode can look up any public Business/Creator account (verified with `nike` and `natgeo`).
5. **No webhook exists** for accounts you don't manage — this is a poll by necessity.

Errors are classified by Meta's error code, not the HTTP status (Meta answers almost everything with 400): an invalid/expired token (190) or missing permission (10) is reported as a credentials problem and never marks monitored accounts invalid; rate-limit codes (4/17/32/613/80002) pause Instagram polling for 15 minutes; only "Cannot find user" (110/2207013) marks an account invalid. At startup the bot checks the token with Meta's `debug_token` and logs a warning if it's invalid, expires within 14 days, or lacks a permission.

### Instagram setup (getting the credentials)

You need your own Instagram **Business or Creator** account linked to a **Facebook Page** you admin, and a Meta app. The bot only uses your token to look other accounts up.

1. **Instagram**: Settings → Account type and tools → *Switch to professional account* (Business or Creator).
2. **Facebook Page**: create one if needed, then in the Page: Settings → *Linked accounts* → Instagram → *Connect*. Linking in Accounts Center alone isn't enough.
3. **Meta app**: developers.facebook.com → *Create app* → use case **Other** → type **Business** → add product **Instagram** → **"API setup with Facebook login"** (not "with Instagram login" — its `IGAA…` tokens can't look up other accounts).
4. **Login configuration** (the app uses *Facebook Login for Business*, which ignores the Graph API Explorer's permission list): *Facebook Login for Business* → *Configurations* → *Create* — access token: **User access token**; assets: **Pages** and **Instagram accounts**; permissions: `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`, `pages_show_list`, `business_management`. `instagram_manage_insights` is required — without it every lookup fails with "(#10) Application does not have permission".
5. **User token**: Graph API Explorer → your app → **Configurations** tab → select the configuration → *Generate Access Token*. Log in as your personal profile and tick your Page and Instagram account. (If the popup only says "name and profile picture", remove the app under Facebook Settings → *Business integrations* and generate again.)
6. **Extend it** — the step that matters: paste the token into the Access Token Debugger (developers.facebook.com/tools/debug/accesstoken) → *Extend Access Token*.
7. **Page token + account ID**: in the Explorer, with the **extended** token, run `me/accounts?fields=name,access_token,instagram_business_account`. The Page's `access_token` → `INSTAGRAM_ACCESS_TOKEN`; `instagram_business_account.id` → `INSTAGRAM_BUSINESS_ACCOUNT_ID`.

8. **Switch the Meta app to Live mode** (the toggle at the top of the App Dashboard). While the app is in *Development* mode the token can be rejected outright — every call, even `GET /me`, fails with "Cannot call API for app … on behalf of user" (seen 2026-10-13 and 2026-10-15; fixed by going Live). If that error ever comes back, check the app's mode first.

A Page token fetched with an extended user token **doesn't expire**. Fetched with the short-lived one, it expires within hours — the startup check warns about exactly that. Meta's separate *data access* window (about 90 days) is renewed whenever you generate a token through the app again; the startup check warns two weeks before it ends.

## Facebook (no adapter)

Meta has no official, ToS-compliant way for this bot to monitor a Page that hasn't individually authorized the app — confirmed by research before writing any code, per the standing platform-API policy:

- **Facebook**: reading a Page's posts/feed always needs that Page's admin to authorize the app (`pages_read_engagement`/`pages_read_user_content`) and pass App Review. The closest exception, "Page Public Content Access," would let an approved app read *any* public Page's posts without that Page's cooperation — but it requires Meta Business Verification (a real identity-verification process) and its only listed allowed use case is "analyze and/or display posts and engagement," which a Discord alert bot fits ambiguously at best. Not built this phase; revisit if the operator completes that verification and review.

## Guild isolation

Every table that stores server-specific data is keyed by `guild_id`, and every query in `database/repositories/` filters by it — there's no code path that loads all guilds' data and filters in Python. `GuildRepository`'s in-memory cache is likewise keyed per guild, so toggling one server can't affect another's cached state. This pattern is the one to follow for every repository added in later phases.

Where one row links two guild-scoped parents — `notify_account_channels` linking a `notify_platform_accounts` row to a `notify_notification_channels` row — the foreign keys are composite (`guild_id`, `id`) rather than plain `id`. That makes it a database-level constraint violation, not just an application bug, to ever wire one guild's account to another guild's channel; `notify_platform_health` is the one exception, since it isn't guild data at all (see the schema section above).

## Security notes

- The Discord token, Supabase key, and all platform credentials are read from environment variables only — never hardcoded, never logged.
- `SUPABASE_KEY` must be the `secret` (or legacy `service_role`) key and must never be shipped to a client or exposed publicly.
- Owner-only commands (`sync`) use `commands.is_owner()`, backed by the bot application's real owner/team (fetched from Discord at startup) plus any IDs in `DISCORD_OWNER_IDS` — not a hardcoded user ID check.
