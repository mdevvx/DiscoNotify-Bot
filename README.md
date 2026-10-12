# PulseNotify

A multi-tenant Discord bot that watches social/streaming accounts (YouTube, Twitch, Kick, X/Twitter, Instagram, Facebook, TikTok) and posts alerts to Discord when they post or go live. Every server's configuration — accounts, channels, alert settings, enabled/disabled state — is fully isolated by `guild_id`.

This is being built in phases. **Phases 1–6 are complete, Phase 7 is partially complete, and Phase 8 is partially complete.** YouTube, Twitch, Kick, X/Twitter, and Instagram are all working end-to-end: `/pulsenotify account-add` registers an account, the monitoring framework polls it, and real Discord notifications go out for new videos/Shorts (YouTube), new posts (X/Twitter, Instagram), and live start/end (YouTube, Twitch, Kick). Facebook and TikTok have **no adapter** — see the Phase 7 section below for why: neither platform offers an official way to monitor an account that hasn't individually authorized this app. Per-account alert configuration (which event types notify, embeds, role mentions) can now be viewed and changed entirely via slash commands — see the Commands section.

## Status

| Phase | What | Status |
|---|---|---|
| 1 | Project structure, config, logging, Discord client, `/pulsenotify status`, `/pulsenotify toggle`, `pn!sync`, Supabase connection | ✅ Done |
| 2 | Full database schema (accounts, channels, alert config, events, live state, platform health) | ✅ Done |
| 3 | Monitoring framework (scheduler, workers, rate limiting, retries, dedup, API usage manager) | ✅ Done — framework only, zero adapters registered |
| 4 | YouTube adapter, `/pulsenotify account-add\|account-remove\|account-list`, notification embeds + sender | ✅ Done |
| 5 | Twitch adapter (live/offline detection) | ✅ Done |
| 6 | Kick adapter (live/offline detection) | ✅ Done |
| 7 | X/Twitter, Instagram, Facebook, TikTok adapters | ⚠️ Partial — X/Twitter and Instagram done (with real caveats, see below); Facebook and TikTok have no official path and were **not built** |
| 8 | `/pulsenotify alerts` (per-event-type enabled/embed toggles + viewing), `/pulsenotify account-set-mention` (role mentions), `/pulsenotify account-info` (view full config incl. custom message) | ⚠️ Partial — analytics and general admin tooling deliberately **skipped**, at the user's explicit choice (2026-09-15) |

### Platform API policy (applies to every platform integration)

Before any platform integration is built: verify that platform's *current* official API pricing, quotas, rate limits, auth/approval requirements, and available endpoints — never assume an API is free, and never build against undocumented/scraped endpoints to route around a restriction. Live monitoring should prefer official webhooks/events (e.g. Twitch EventSub) over polling wherever a platform actually offers them — the monitoring framework built in Phase 3 is polling-shaped (`PlatformScheduler` on an interval), so a webhook-based platform will need its own inbound-delivery path (see Monitoring framework below) rather than being forced through `fetch_updates()`.

- **YouTube's live-stream detection** was a deliberate exception, made with the user's explicit sign-off: there's no cheap official alternative to the 100-quota-unit `search.list` call without a public HTTPS webhook endpoint this project doesn't have yet — see the YouTube adapter section below for the exact tradeoff and how it's budgeted.
- **Twitch (Phase 5) and Kick (Phase 6)** were deliberate polling choices too, also with sign-off where the tradeoff was real: EventSub-style webhooks are the "correct" push-based approach for both, but need a public HTTPS endpoint this project doesn't have; Twitch's EventSub WebSocket transport (which would avoid that) turned out to require a *user* access token capped far too low for monitoring arbitrary streamers, so it isn't viable at all. Polling is cheap enough on both platforms' free app-token tiers that there's no quota pressure forcing the webhook infrastructure question the way there was for YouTube.
- **X/Twitter (Phase 7) has no free tier at all as of 2026** — reading posts is billed pay-per-use with no free allowance. This adapter is built, but enabling it (`TWITTER_BEARER_TOKEN`) is an explicit opt-in to a real, ongoing dollar cost, unlike every other platform here. See the X (Twitter) adapter section below before turning it on.
- **Instagram (Phase 7)** has no equivalent to Twitch/Kick's "any public account, no consent needed" model. The only officially-supported path that doesn't require the *target* account's cooperation — Business Discovery — only works against Business/Creator accounts (not personal ones), requires the operator's own qualifying Meta app + linked professional account, and requires that app to pass Meta App Review before it can query anyone beyond its own testers. See the Instagram adapter section below.
- **Facebook and TikTok (Phase 7) have no adapter.** Both platforms' official APIs are built entirely around a creator/Page connecting *their own* account to your app — the opposite of monitoring one you don't control — with no Instagram-Business-Discovery-style exception for either. Facebook's closest equivalent ("Page Public Content Access") requires Meta Business Verification and is scoped to "analyze/display" use cases with no guarantee a notification bot qualifies; TikTok's developer platform has no discovery/monitoring surface at all. Building against either would mean scraping, which the standing policy above explicitly rules out. Revisit if either platform's policy changes.

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
   - `TWITTER_BEARER_TOKEN` — an app-only Bearer token from the [X Developer Portal](https://developer.x.com). **Costs real money to use — no free tier as of 2026** (see the X (Twitter) adapter section below before enabling).
   - `INSTAGRAM_ACCESS_TOKEN` / `INSTAGRAM_BUSINESS_ACCOUNT_ID` — a long-lived token + IG user ID for your *own* linked Instagram professional account, from a [Meta app](https://developers.facebook.com/apps). Requires Meta App Review before it can monitor accounts beyond your own testers, and only works against Business/Creator target accounts (see the Instagram adapter section below).
   - There's no Facebook or TikTok key to set — neither has an adapter (see the Phase 7 section above).

   `MONITORING_*`/`YOUTUBE_*` tuning variables (polling intervals, retry/backoff, rate limits, health thresholds, quota budgets) are all optional too — sensible defaults are baked in; see `.env.example` for the full list.

3. **Run the database migrations**

   In the Supabase SQL editor, run, in order:
   - [database/migrations/0001_create_guilds.sql](database/migrations/0001_create_guilds.sql)
   - [database/migrations/0002_core_schema.sql](database/migrations/0002_core_schema.sql)
   - [database/migrations/0003_add_content_cursor.sql](database/migrations/0003_add_content_cursor.sql)
   - [database/migrations/0004_add_webhook_url.sql](database/migrations/0004_add_webhook_url.sql)
   - [database/migrations/0005_move_to_pulsenotify_schema.sql](database/migrations/0005_move_to_pulsenotify_schema.sql)

   Then add `pulsenotify` under **Project Settings > Data API > Exposed schemas**. All tables live in that schema, not `public`, and the bot can't reach them until the schema is exposed.

   More migrations are added as later phases need them.

4. **Invite the bot**

   Generate an invite URL in the Developer Portal with the `bot` and `applications.commands` scopes, and at least `Send Messages`, `Embed Links`, `Use Slash Commands`, `Manage Webhooks` (notifications are delivered through a per-channel webhook, not the bot's own user — see the Monitoring framework section below; without this permission, the bot falls back to sending as itself instead).

5. **Run it**

   ```bash
   python main.py
   ```

6. **Sync slash commands**

   Slash commands aren't visible in Discord until synced. In any server the bot is in (as the bot owner), run:

   - `pn!sync` — syncs to that server only, visible immediately (use while developing)
   - `pn!sync global` — syncs everywhere, can take up to an hour to propagate (use for production releases)

   (`pn!` is the default prefix, set by `DISCORD_COMMAND_PREFIX` in `.env`.)

## Commands

Every slash command lives under one top-level `/pulsenotify` group (`commands/pulsenotify_group.py`) — there is no bare `/status` or `/toggle`. Account management (`account-add`/`account-remove`/`account-list`) is flattened into direct subcommands of the same group rather than nested under its own `account` subgroup: Discord doesn't allow mixing plain subcommands and subcommand groups as siblings under one parent, and `/pulsenotify toggle` (a plain subcommand) was the pattern to match.

| Command | Description | Permission |
|---|---|---|
| `/pulsenotify status` | Bot health: uptime, latency, server count, database connection, this server's enabled state, per-platform monitoring health/request counts | Anyone |
| `/pulsenotify help` | Lists every command and what it does — built by introspecting the command group itself, so it can't drift out of sync with what's actually registered | Anyone |
| `/pulsenotify toggle state:<on\|off>` | Enable/disable PulseNotify for this server. Everything else is blocked while disabled — `toggle` itself always still works. | Manage Server |
| `/pulsenotify account-add platform identifier channel` | Start monitoring a platform account; posts alerts to the given channel | Manage Server |
| `/pulsenotify account-remove account` | Stop monitoring an account (autocompletes from this server's configured accounts) | Manage Server |
| `/pulsenotify account-list` | List accounts monitored in this server, with status | Anyone |
| `/pulsenotify account-set-message account message [event_type]` | Set a custom notification message template for an account (or `clear` to remove it); see below | Manage Server |
| `/pulsenotify account-set-mention account [role] [event_type]` | Set the role to mention when an account posts/goes live (omit `role` to clear it) | Manage Server |
| `/pulsenotify alerts account event_type [enabled] [embed]` | View an event type's current alert/embed setting (omit both `enabled` and `embed`), or change one/both | Manage Server |
| `/pulsenotify account-info account` | Show an account's full alert configuration — every event type's enabled/embed/mention state, plus its custom message if one is set | Anyone |
| `pn!sync [global]` | Sync slash commands to this server, or globally. A text command, not a slash command — if the command tree is out of sync, `/pulsenotify` itself might not be registered yet, so syncing can't depend on it. | Bot owner only |

Per-account alert preferences (which event types notify, mentions, embed on/off) are seeded with sensible defaults when an account is added — new posts/videos/Shorts/live-start on, stream-ended off, matching the spec's own example. All of them are now viewable and changeable via slash commands (Phase 8): `/pulsenotify alerts` turns an event type's notifications and/or embed on or off (and shows the current setting if you omit both), `/pulsenotify account-set-mention` sets or clears the role pinged for an account, and `/pulsenotify account-info` shows everything for an account at a glance — including the custom message, which previously could only be *set* (`account-set-message`), never viewed back.

Deliberately **not** built in Phase 8, at the user's explicit choice: analytics (event-count/history stats) and general admin tooling (e.g. wiring up `pulsenotify_audit_logs`, which still exists but nothing writes to it). Revisit if wanted later.

### Custom notification messages

`/pulsenotify account-set-message` lets an admin replace the default embed/text with their own template, e.g.:

```
Hey @everyone, {creator} just posted a new post! Go check it out!
{url}
```

Placeholders (`notifications/templates.py`): `{creator}` `{title}` `{description}` `{url}` `{platform}` `{category}` `{viewers}` `{mention}` (the account's configured mention role, if any — empty string otherwise). An unrecognized `{placeholder}` is left as literal text rather than erroring, and the rendered result is truncated to Discord's 2000-character message limit if substitution pushes it over — a malformed template can never crash delivery.

Two different trust levels are handled on purpose: the **template** is admin-authored (the command requires Manage Server) and may contain literal `@everyone`, `@here`, or role mentions — that's explicit configuration, matching the example above. The **values substituted into it** (video title, post description, ...) come from external platform content and are always mention-escaped first (`discord.utils.escape_mentions`), so a stream title containing the literal text "@everyone" can never inject a mention the admin didn't type themselves. `AllowedMentions.everyone` is only set `True` when the *rendered* content actually contains a literal `@everyone`/`@here` — which, given the escaping above, can only happen if the admin put it in the template.

By default a custom message replaces the standard notification text entirely; the account's `embed_enabled` setting still independently controls whether Discord's own native preview of the `{url}` link is allowed to render alongside it (see "Notifications never build their own embed" below).

### Notifications never build their own embed

Every notification (default text or a custom message) is sent as plain text containing the event's raw link — never a hand-built `discord.Embed`. Discord generates its own preview by unfurling that link, and for platforms like YouTube that preview is richer and more accurately branded (real title/description/thumbnail, a "YouTube" footer) than anything built by hand here could be. `embed_enabled` (`/pulsenotify alerts`) controls whether that native preview is allowed to show at all (`suppress_embeds=False`) or is suppressed entirely (`suppress_embeds=True`) — it no longer toggles between "custom embed" and "plain text," since there's no custom embed to toggle. Fixed 2026-09-18 after visual proof (a real notification screenshot) that the previous hand-built embed looked noticeably worse than Discord's own YouTube link preview.

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
  client.py           PulseNotifyBot (discord.ext.commands.Bot subclass), command tree gating, owns MonitoringManager
  events.py            Discord event listeners + centralized error handling
  lifecycle.py          process run/shutdown loop (signal handling, graceful close)
commands/
  pulsenotify_group.py   the single shared `/pulsenotify` app_commands.Group every subcommand below attaches to
  admin/                status.py, toggle.py, help.py — decorate against the shared group; sync.py — a real Cog (text command, not a slash command)
  accounts/              account_commands.py — account-add/-remove/-list/-set-message/-set-mention/-info, alerts; flattened into the shared group; see its docstring for why
platforms/
  base.py              PlatformAdapter interface every platform implements; event_types_for_capabilities() helper
  youtube/               the first concrete adapter — client.py (HTTP), parser.py (pure JSON->shapes), adapter.py (ties them together + quota budgeting)
  twitch/                 live/offline only — app-token polling, no adapter-level quota budgeting needed (see Twitch adapter section)
  kick/                   live/offline only — same shape as twitch/, one endpoint does both identifier resolution and live status
  twitter/                new-post detection — costs real money to enable, see X (Twitter) adapter section
  instagram/              new-post detection via Business Discovery — real setup/review constraints, see Instagram adapter section
monitoring/
  manager.py            single start()/stop() entry point; owns one PlatformScheduler per registered adapter
  scheduler.py           polls one platform's enabled accounts on an interval, shared rate limiter
  worker.py               polls one account: fetch/normalize, live-transition detection, error isolation
  rate_limiter.py         sliding-window async limiter, shared per-platform, cost-weighted (see YouTube section)
  retry.py                 exponential backoff; skips retrying permanent/rate-limit errors
  health.py                per-platform health/usage tracker (batches request counts, gates on rate limits)
events/
  models.py             NormalizedEvent, EventType — the common event shape every adapter produces
  processor.py            queue + consumer: dedup against `pulsenotify_events`, then hands new events to a sink
notifications/
  embeds.py              NormalizedEvent -> plain notification text (no custom discord.Embed — Discord's own link preview handles that, see README)
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
  errors.py              PulseNotify's exception hierarchy (PlatformError, RateLimitedError, ...)
tests/
  unit/                  fast tests, no live external services
  integration/            multiple real components wired together, still no live external services
```

## Database schema

Three migrations exist so far:

- **`0001_create_guilds.sql`** — `pulsenotify_guilds`: one row per server, tracking only its enabled/disabled state (Phase 1).
- **`0002_core_schema.sql`** — everything needed to configure and run monitoring:
  - `pulsenotify_bot_settings` — guild-wide preferences (1:1 with `pulsenotify_guilds`); not used by any command yet
  - `pulsenotify_platform_accounts` — one row per monitored account per guild; duplicate (guild, platform, account) prevented by a unique constraint; created by `/pulsenotify account-add`
  - `pulsenotify_notification_channels` — Discord channels registered to receive alerts; marked `is_valid = false` rather than deleted if the bot loses access
  - `pulsenotify_account_channels` — routes one account's alerts to one channel (many-to-many)
  - `pulsenotify_alert_configurations` — per-account, per-event-type toggles (new post, live started, mentions, embeds, ...); seeded with defaults by `/pulsenotify account-add`
  - `pulsenotify_events` — the deduplication ledger: `(account_id, event_type, platform_event_id)` is unique, checked before ever sending a notification
  - `pulsenotify_live_states` — current live/offline status per account, so a bot restart mid-stream doesn't re-announce it
  - `pulsenotify_audit_logs` — append-only record of administrative actions; not written by anything yet
  - `pulsenotify_platform_health` — **not** guild-scoped; one row per platform (YouTube, Twitch, ...) tracking the bot's own API usage/quota/rate-limit state against that provider — created on first use
- **`0003_add_content_cursor.sql`** — adds `pulsenotify_platform_accounts.content_cursor`, the persisted "newest content seen" pointer. Found and fixed during Phase 4: without persisting it, a bot restart reset an account's cursor to NULL, which would either re-flood the channel's entire back-catalog or silently skip whatever was published during the downtime, depending on how that case was handled. See `monitoring/worker.py`'s docstring.
- **`0004_add_webhook_url.sql`** — adds `pulsenotify_notification_channels.webhook_url`, so the per-channel Discord webhook notifications are delivered through (see Monitoring framework below) is created once and reused across restarts instead of a new one appearing every time the bot starts up.
- **`0005_move_to_pulsenotify_schema.sql`** — moves every table, domain and the `updated_at` trigger function out of `public` into a dedicated `pulsenotify` schema, and grants `service_role` access to it. The bot's client is pinned to that schema (`DB_SCHEMA` in `database/client.py`), and the schema has to be listed under the Data API's exposed schemas.

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
                  dedup against `pulsenotify_events` (unique on account+type+platform_event_id)
                            ↓
                   new? → notification sink (NotificationSender, see below)
```

What's real and tested:

- **`PlatformAdapter`** ([platforms/base.py](platforms/base.py)) — the interface a concrete adapter implements: `validate_account`, `fetch_updates`, `get_live_status`, `normalize_event`, plus a `capabilities` set (`POSTS`, `VIDEOS`, `SHORTS`, `STREAMS`, `LIVE_STATUS`, `CHANNEL_UPDATES`) so the worker only calls what a platform actually supports.
- **Rate limiting** — one `RateLimiter` (sliding window) per platform, shared by every account worker for that platform, since the limit is against the platform's API as a whole. It's cost-weighted (`acquire(cost=N)`/`try_acquire(cost=N)`), not just a request count — added specifically because YouTube's API charges wildly different costs per endpoint (see below).
- **Retries** — exponential backoff with jitter; a `PermanentPlatformError` (account gone) or `RateLimitedError` (429/quota) is never retried within a call — see below.
- **The 429 / quota-exhaustion path** — an adapter raises `RateLimitedError(message, retry_after=...)`; the worker routes that to `PlatformHealthTracker.record_rate_limited()` instead of counting it against the account, and `PlatformScheduler` checks `is_rate_limited()` before every poll cycle and skips that platform entirely until the window passes. One platform hitting its quota never touches any other platform's scheduler.
- **API usage tracking** — `PlatformHealthTracker` batches request counts in memory and flushes to the (not guild-scoped) `pulsenotify_platform_health` table once per scheduler tick; surfaced in `/pulsenotify status`.
- **Live-transition detection** — the worker, not the adapter, compares a `LiveStatus` snapshot against the persisted `pulsenotify_live_states` row to decide whether to fire `STREAM_STARTED`/`STREAM_ENDED` — this is what makes "don't re-announce a stream that was already live before a restart" (section 8) work for every platform for free, rather than every adapter reimplementing it.
- **Deduplication** — `EventProcessor` is the one place that checks `pulsenotify_events` and decides "new" vs "already seen," so that guarantee holds regardless of which worker or platform produced the event.
- **Notification delivery** — `EventProcessor`'s sink is `NotificationSender.handle` ([notifications/sender.py](notifications/sender.py)): checks the account's alert config for that event type, builds the plain notification text (never a custom embed — see "Notifications never build their own embed" above), scopes mentions to just the configured role (`AllowedMentions`, never `@everyone`/arbitrary users — section 36), and sends to every channel linked to that account. A channel Discord says is gone (`Forbidden`/`NotFound`) gets marked `is_valid=false` instead of retried forever (section 23); other Discord errors are logged and skipped, not treated as fatal.
- **Delivery is via a per-channel webhook, not the bot's own user.** `/pulsenotify account-add` creates the channel's webhook right away (not waiting for the first alert), specifically so the admin can rename it / give it a custom avatar in Discord's own channel-integrations UI before anything is ever sent through it — and, as of 2026-09-18, every send actually respects that customization: `webhook.send()` never passes a per-message `username`/`avatar_url` override, so the webhook keeps displaying whatever name/avatar it's actually configured with (the "PulseNotify" default from creation, or the admin's own rebrand) instead of being silently stomped back to the raw platform account name on every single notification — a real bug (confirmed via a live screenshot showing an admin-customized webhook reverting to the raw `@handle` name) fixed the same day. Its URL is persisted on the channel row (`webhook_url`, migration 0004) and reused after that — never a new webhook per message, and never a second one for a channel that already has one, even across multiple accounts/multiple sends. Before trusting a stored URL, `NotificationSender.get_or_create_webhook()` actually verifies with Discord that the webhook still exists (`webhook.fetch()`) — found necessary in practice: a webhook can be deleted out from under the bot by a server admin or some unrelated automation/integration, and without this check a stale URL would just get handed back forever, silently, with nothing ever getting (re)created. If the bot lacks "Manage Webhooks" in a channel, or webhook delivery fails outright, it falls back to sending as the bot itself rather than dropping the alert.

**Known architectural gap:** this framework is polling-shaped — `PlatformScheduler` calls `fetch_updates()`/`get_live_status()` on an interval. The platform-API policy above prefers webhooks (Twitch EventSub, YouTube WebSub) where a platform offers them and the cost of polling is too high; a webhook-based platform will need its own inbound HTTP receiver feeding events into the same `EventProcessor.submit()`, bypassing the polling scheduler rather than being forced through it. Not designed yet.

## YouTube adapter (platforms/youtube/)

The first concrete `PlatformAdapter`. API availability was verified against Google's current documentation before writing any code (see `platforms/youtube/adapter.py`'s module docstring for the full table) — summary:

| Feature | Method | Cost | Notes |
|---|---|---|---|
| New videos | `playlistItems.list` on the uploads playlist | 1 unit | Reliable, cheap |
| Shorts | `contentDetails.duration <= 60s` heuristic | (included above) | **No official "is Short" field exists.** False positives possible for short regular videos — `NormalizedEvent.metadata["is_short_heuristic"]` marks it as a guess, not fact |
| Community posts | — | — | **Not implemented — no public API endpoint exists for this at all** |
| Live start/end | `search.list(eventType=live)` + `videos.list` | 100 + 1 | The *only* documented way to find a live broadcast on a channel this bot doesn't own |
| Upcoming/scheduled streams | — | — | Not implemented this phase |

Credentials: a plain API key, no OAuth (everything above is public data).

**The quota tradeoff, and why it needed your sign-off:** the free tier is 10,000 units/day, shared by every YouTube account this bot monitors — one budget per API key, not per account or guild. `search.list` alone can burn the entire daily budget checking live status for a single channel every ~15 minutes. Given the choice between (a) shipping video detection only and deferring live detection, (b) accepting the quota cost, or (c) building a WebSub push receiver now, **you chose (b)** — live detection ships, budgeted explicitly rather than left to accidentally exhaust the quota:

- `YOUTUBE_DAILY_QUOTA_UNITS` (default 10,000) and `YOUTUBE_LIVE_SEARCH_DAILY_BUDGET_UNITS` (default 8,000) split the daily budget between the two call types.
- The adapter owns two internal `RateLimiter`s sized from those settings — one for the cheap 1-unit calls, one for the 100-unit live search.
- The live-search limiter uses `try_acquire()` (non-blocking): once the day's live-search budget is spent, `get_live_status()` just returns `None` (skip this cycle, not an error) instead of blocking a worker for hours waiting for the window to free up.
- A real 429 or `quotaExceeded` response additionally pauses that entire platform via the framework's rate-limit path described above.

If you later get a Google quota extension, raise `YOUTUBE_DAILY_QUOTA_UNITS` accordingly — you cannot pay to increase it directly.

**Scheduler cadence fix (2026-09-16):** `MonitoringManager` picks one poll interval per platform based only on whether it supports `Capability.LIVE_STATUS` — 60s (`MONITORING_LIVE_POLL_INTERVAL_SECONDS`) for any such adapter, since that's fine for Twitch/Kick's cheap live checks. YouTube also has `LIVE_STATUS`, so it was getting scheduled every 60s too — but its search budget (80 calls/day at the defaults above) was designed to be spread roughly once every 18 minutes, not burned in the first 80 minutes after every restart and then gone dark for the rest of the day. `PlatformAdapter` now has an optional `min_poll_interval_seconds` an adapter can set to floor its own scheduler cadence; `YouTubeAdapter` computes it from `YOUTUBE_LIVE_SEARCH_DAILY_BUDGET_UNITS` (86400s / (budget / 100 units per call)), so it stays correct if that setting is ever tuned. This also slows down new-video polling to the same ~18-minute cadence (both content and live checks share one scheduler tick per account) — an acceptable tradeoff since new-video alerts were never latency-sensitive to the minute.

**Per-channel live-status caching (2026-09-18):** the fix above sized the ~18-minute cadence assuming exactly one YouTube channel amortizes the whole daily search budget — which breaks the moment a second distinct channel (or a second guild monitoring the *same* channel) shares this one adapter's budget, since `PlatformScheduler` polls every registered account on the same tick regardless of platform_account_id. Found in production: two guilds each monitoring the same creator meant that one physical channel was being charged for two 100-unit `search.list` calls per tick instead of one. `YouTubeAdapter.get_live_status()` now caches its result per `platform_account_id` (not per guild-scoped account row), with a window that grows with the number of *distinct* channels seen so far (`min_poll_interval_seconds × known_channel_count`) — so N channels sharing the budget each get checked proportionally less often instead of collectively exhausting the shared budget partway through the day. The known-channel count only ever grows within one adapter instance's lifetime (an account removed later still counts until the bot restarts), a deliberately conservative choice: it can only make checks somewhat slower than the theoretical minimum, never push total usage over budget. `YouTubeAdapter` takes an injectable `clock` (`time.monotonic` by default) purely so this is testable without real sleeps.

## Twitch adapter (platforms/twitch/)

Live/offline detection only (`Capability.LIVE_STATUS`) — see `platforms/twitch/adapter.py`'s module docstring for the full research writeup. Summary:

| Feature | Method | Auth | Notes |
|---|---|---|---|
| Live/offline | `GET /helix/streams?user_id=` | app access token | No broadcaster authorization needed — this event requires no scope. |
| Channel resolution | `GET /helix/users?login=` | app access token | Resolves a login/URL to a stable numeric user ID. |

Credentials: `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`, an app access token via OAuth client-credentials — no per-broadcaster authorization, no OAuth redirect flow. Twitch's app-token bucket is 800 points/minute and this adapter spends 1 point/account/poll, so unlike YouTube no adapter-level quota budgeting was needed — the framework's own generic per-platform `RateLimiter` (`MONITORING_RATE_LIMIT_REQUESTS`/`_PERIOD_SECONDS`, default 30/60s) is already far more conservative.

**Why polling instead of EventSub, and why that needed sign-off:** EventSub's WebSocket transport would avoid needing a public HTTPS endpoint, but requires a *user* access token capped at a total subscription cost of 10 per (client_id, user) — nowhere near enough for monitoring arbitrary streamers who haven't authorized this app. EventSub's webhook transport works fine with an app token and a 10,000-cost ceiling, but needs a publicly reachable HTTPS callback (valid TLS, port 443) this project doesn't have. Given the choice between (a) polling now or (b) building that public endpoint first, **the user chose (a)** — Twitch's cost/scale here makes polling cheap enough that there's no pressure to build the webhook infrastructure.

## Kick adapter (platforms/kick/)

Live/offline detection only, same shape as the Twitch adapter — see `platforms/kick/adapter.py`'s module docstring. Kick's Public API (docs.kick.com) is close enough to Twitch's own API/OAuth shape that the same polling decision and client structure apply.

| Feature | Method | Auth | Notes |
|---|---|---|---|
| Live/offline + channel resolution | `GET /public/v1/channels?slug=` or `?broadcaster_user_id=` | app access token | No broadcaster authorization needed. One endpoint returns both identity and live status (title/category/viewer_count/thumbnail/url) — unlike Twitch, no separate "users" endpoint is needed. |

Credentials: `KICK_CLIENT_ID`/`KICK_CLIENT_SECRET`, an app access token via OAuth client-credentials — same model as Twitch, described by Kick's own docs as usable "when user login is not required."

**One real difference from every other live-status platform: Kick's API exposes no per-session stream ID.** YouTube's video ID and Twitch's `stream.id` both uniquely identify one broadcast session; Kick's channel resource has nothing equivalent. `monitoring/worker.py`'s `platform_event_id` fallback for a missing `stream_id` was fixed during this phase to incorporate `started_at` (not just the account ID) specifically because of this — the original fallback (`f"live-{account_id}"`) would have been the *same string* every time that account went live again, and since the events table's dedup key is `(account_id, event_type, platform_event_id)`, every live notification after an account's first one would have been silently swallowed as "already seen." See `tests/unit/test_worker.py::test_two_separate_live_sessions_without_a_stream_id_get_different_event_ids` for the regression test.

## X (Twitter) adapter (platforms/twitter/)

**Read this before enabling it.** X discontinued its free API tier in February 2026. Reading posts is now billed pay-per-use (roughly $0.005/read, capped at 2,000,000 reads/month) with no free allowance; a legacy fixed-price Basic plan ($200/month) still exists but only for pre-existing subscribers. Setting `TWITTER_BEARER_TOKEN` is an explicit opt-in to a real, ongoing dollar cost that scales with how many accounts you monitor and how often (`MONITORING_CONTENT_POLL_INTERVAL_SECONDS`) — unlike every other platform in this project, which are all free.

| Feature | Method | Notes |
|---|---|---|
| New posts | `GET /2/users/{id}/tweets` with a `since_id` cursor | Retweets/replies excluded; requests the API's minimum page size (5) per poll to limit billed reads. |
| Handle resolution | `GET /2/users/by/username/{username}` | No target authorization needed — public profile data. |

Credentials: `TWITTER_BEARER_TOKEN`, a static app-only Bearer token generated once in the X Developer Portal — no OAuth dance, no per-account authorization, no in-app refresh (same operational model as YouTube's API key). No adapter-level rate limiting beyond the framework's generic per-platform `RateLimiter` — X's constraint here is the operator's own budget, not a platform-side wall this code can safely auto-throttle against.

## Instagram adapter (platforms/instagram/)

**Read this before enabling it — real setup and policy constraints, not just an API key.** As of 2026, Meta's official APIs don't allow discovering or reading an arbitrary Instagram account's content the way Twitch/Kick's app-token model does. The only officially-supported path that doesn't require the *target* account's cooperation is **Business Discovery**:

| Feature | Method | Auth | Notes |
|---|---|---|---|
| Profile + recent media | `GET /{your_ig_user_id}?fields=business_discovery.username(...)` | Long-lived access token for **your own** linked IG professional account | No authorization from the target account — but see the constraints below. |

Real constraints, unlike every other adapter in this project:
1. **The target must itself be a Business or Creator account** — a personal Instagram account can't be monitored this way at all. A hard capability boundary, not a heuristic gap.
2. **The operator needs their own qualifying setup**: a Meta Developer app, a Facebook Page, and their own Instagram Business/Creator account connected to it — that's where `INSTAGRAM_ACCESS_TOKEN`/`INSTAGRAM_BUSINESS_ACCOUNT_ID` come from.
3. **Meta App Review is required before this works against arbitrary accounts.** In Development mode, Business Discovery only succeeds against accounts added as testers on the operator's own app.
4. **Lookup is by username only** — there's no by-ID variant. If a monitored account renames its handle, polling breaks until the account is removed and re-added.
5. **No webhook exists for this** — it's a straightforward poll; Instagram's webhook system only covers accounts your own app manages.

Credentials renew manually roughly every 60 days — no in-app OAuth refresh flow is implemented, the same trade-off as YouTube's static API key.

## Facebook and TikTok (no adapter)

Neither platform has an official, ToS-compliant way for this bot to monitor a Page/creator that hasn't individually authorized the app — confirmed by research before writing any code, per the standing platform-API policy:

- **Facebook**: reading a Page's posts/feed always needs that Page's admin to authorize the app (`pages_read_engagement`/`pages_read_user_content`) and pass App Review. The closest exception, "Page Public Content Access," would let an approved app read *any* public Page's posts without that Page's cooperation — but it requires Meta Business Verification (a real identity-verification process) and its only listed allowed use case is "analyze and/or display posts and engagement," which a Discord alert bot fits ambiguously at best. Not built this phase; revisit if the operator completes that verification and review.
- **TikTok**: every official developer surface (the Content Posting API and everything else) is built around a creator connecting *their own* account to your app — the opposite of monitoring a creator you don't control. No discovery/monitoring endpoint exists at all.

Building against either via scraping was considered and rejected — the standing platform-API policy explicitly rules out undocumented/scraped endpoints regardless of how much faster it would ship.

## Guild isolation

Every table that stores server-specific data is keyed by `guild_id`, and every query in `database/repositories/` filters by it — there's no code path that loads all guilds' data and filters in Python. `GuildRepository`'s in-memory cache is likewise keyed per guild, so toggling one server can't affect another's cached state. This pattern is the one to follow for every repository added in later phases.

Where one row links two guild-scoped parents — `pulsenotify_account_channels` linking a `pulsenotify_platform_accounts` row to a `pulsenotify_notification_channels` row — the foreign keys are composite (`guild_id`, `id`) rather than plain `id`. That makes it a database-level constraint violation, not just an application bug, to ever wire one guild's account to another guild's channel; `pulsenotify_platform_health` is the one exception, since it isn't guild data at all (see the schema section above).

## Security notes

- The Discord token, Supabase key, and all platform credentials are read from environment variables only — never hardcoded, never logged.
- `SUPABASE_KEY` must be the `secret` (or legacy `service_role`) key and must never be shipped to a client or exposed publicly.
- Owner-only commands (`sync`) use `commands.is_owner()`, backed by the bot application's real owner/team (fetched from Discord at startup) plus any IDs in `DISCORD_OWNER_IDS` — not a hardcoded user ID check.
