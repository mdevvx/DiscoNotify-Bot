# Website integration — subscribing on the PulseNotify website

This is the spec for the website side of "website mode": people subscribe on
your website, get a Polar checkout link, pay, and come back to a page that
shows a **success, warning or error** alert. The bot needs no inbound port:
it finds the payment by itself within about a minute.

Website mode is on when the bot has **both** `BILLING_SUBSCRIBE_URL` (this
site's subscribe page) and `BILLING_LINK_SECRET` set. Without them the bot
creates card checkouts itself and none of this is needed.

There are **two ways in**, and both end at the same checkout:

- **A. From Discord** — `/pulsenotify subscribe` gives a signed link: the server is already chosen and the person already identified (section 1–2).
- **B. Straight from the website** — "Log in with Discord", then **pick your server from a list** of the servers you manage that the bot is in. No server IDs to type (section 2B).

```
/pulsenotify subscribe ──► "Subscribe on the website" button (signed link)
        │
        ▼
 GET /subscribe?t=<token>     verify token → show server, price, trial → [Pay by card]
        │
        ▼
 POST /api/checkout           verify token → create Polar checkout → redirect to Polar
        │
   customer pays on Polar
        │
        ▼
 GET /thanks?checkout_id=…    ask Polar for the result → success / warning / error alert
        │
        ▼
 the bot lists Polar's subscriptions (≤ 1 min), activates the server and posts
 "✅ Payment received" in the channel where /pulsenotify subscribe was used
```

## What the website needs (server side — a static page can't do this)

| Setting | Value |
|---|---|
| `BILLING_LINK_SECRET` | The **same** random string as the bot's. Never sent to the browser. |
| `POLAR_WEB_TOKEN` | A Polar **organization access token** with only `checkouts:write` and `checkouts:read`. Make a separate one — not the bot's token (that one can open billing portals). |
| `POLAR_PRODUCT_ID` | The same monthly product the bot uses. |
| `SITE_URL` | e.g. `https://pulsenotify.duoplus.org` |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | Only for the server picker (way B). The bot's Discord application → OAuth2: add the redirect `{SITE_URL}/auth/callback` and copy/reset the client secret. Server side only. |
| `SESSION_SECRET` | A long random string for signing the login session cookie (way B). |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Only for the optional "activating your server…" step below. Server side only. |

Any backend works (a serverless function, Express, Flask…). The Node
examples below are a reference.

## 1. The signed token

`BILLING_SUBSCRIBE_URL?t=<token>`; valid for 30 minutes (`BILLING_LINK_TTL_MINUTES`).

```
token     = payload + "." + signature
payload   = base64url(JSON), no padding
signature = base64url(HMAC-SHA256(secret, payload)), no padding   // the payload string exactly as in the token
```

JSON fields: `v` (always 1), `g` server ID, `u` user ID (the person who ran the
command; may be missing), `c` channel ID (may be missing), `n` server name
(may be missing), `d` free-trial days left (0 = none), `e` expiry (unix
seconds). **IDs are strings** — don't parse them as numbers in JavaScript.

```js
const crypto = require("crypto");

function verifyToken(token, secret) {
  const [payload, signature, extra] = String(token || "").split(".");
  if (!payload || !signature || extra !== undefined) return null;
  const expected = crypto.createHmac("sha256", secret).update(payload).digest("base64url");
  const a = Buffer.from(signature), b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;      // forged
  let data;
  try { data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")); } catch { return null; }
  if (data.v !== 1 || !/^\d{1,19}$/.test(data.g) || data.e < Date.now() / 1000) return null; // wrong version / expired
  return data;
}
```

Always verify on the server, on **every** request that uses the token.

## 2. `GET /subscribe?t=…` — the page (way A)

- Token invalid or expired → **warning** alert: "This link has expired. Run `/pulsenotify subscribe` in your server for a new one."
- Valid → show the server name (`n`) — **HTML-escape it**: it's whatever the server's admin typed — the price ($2.99/month), and if `d > 0`: "Free trial: your card is saved now and **nothing is charged for `d` more days**. Cancel before then with `/pulsenotify cancel` and you pay nothing."
- A **Pay by card** button (a form that POSTs the token to `/api/checkout`) and, if you offer crypto, **Pay with crypto**.
- Links to Terms, Privacy and Refund Policy next to the button.

## 2B. Way B — Log in with Discord and pick a server

When someone opens `/subscribe` **without** a token, show **"Log in with Discord"** instead of an
ID box. After login they see only the servers they can manage that PulseNotify is in, each with
its subscription state, and choose one.

1. **Redirect to Discord:** `https://discord.com/oauth2/authorize?client_id=…&response_type=code&scope=identify%20guilds&redirect_uri={SITE_URL}/auth/callback&state=<random>&prompt=none`. Keep `state` in a short-lived `HttpOnly` cookie and check it on return (stops login CSRF).
2. **Callback (server side):** exchange the `code` — `POST https://discord.com/api/oauth2/token` (form: `client_id`, `client_secret`, `grant_type=authorization_code`, `code`, `redirect_uri`) — then call `GET https://discord.com/api/users/@me` and `GET https://discord.com/api/users/@me/guilds` with `Authorization: Bearer <access_token>`. Then **revoke the token** (`POST https://discord.com/api/oauth2/token/revoke`) — you only need it for these two calls.
3. **Keep only the servers they can manage** — owner, **Manage Server** or **Administrator**, the same rule the bot applies:

```js
const MANAGE_GUILD = 0x20n, ADMINISTRATOR = 0x8n;
const canManage = (g) => g.owner || (BigInt(g.permissions) & (MANAGE_GUILD | ADMINISTRATOR)) !== 0n;
const manageable = guilds.filter(canManage);          // g.id, g.name, g.icon; permissions is a string
```

4. **Which of them have the bot, and their state** — two reads with the Supabase service key, server side (header `Accept-Profile: pulsenotify`):

```
GET {SUPABASE_URL}/rest/v1/pulsenotify_guilds?guild_id=in.(id1,id2,…)&bot_present=eq.true&select=guild_id
GET {SUPABASE_URL}/rest/v1/pulsenotify_billing?guild_id=in.(id1,id2,…)&select=guild_id,trial_ends_at,paid_until,whitelisted,card_status,card_cancels_at
```

   - Servers **without** the bot: show **Add PulseNotify** → `https://discord.com/oauth2/authorize?client_id=…&scope=bot%20applications.commands&permissions=537021440&guild_id=<id>&disable_guild_select=true`.
   - Servers **with** the bot: show the state — "Free trial — N days left", "Active until …", "Whitelisted", or "No subscription" — and a **Subscribe** button, **disabled** when `card_status` is `trialing`/`active`/`past_due` without `card_cancels_at` ("Already subscribed — manage it with `/pulsenotify cancel` in Discord"). Duplicate subscriptions mean double charges.
   - Trial days for the checkout: `max(0, ceil((trial_ends_at − now) / 1 day))`; no billing row yet → the full trial (7).
5. **Session:** store in a server-side session (cookie `HttpOnly; Secure; SameSite=Lax`, ~30 minutes): the user's ID and name, and the list of server IDs they may subscribe. **Never trust a server ID from the browser** — `/api/checkout` must check it against that list.
6. They choose a server → the page posts `guild_id` to `/api/checkout` (section 3). The metadata gets `guild_id` and `user_id` (from the login — so the bot thanks them, and they can open the billing portal); there's no channel, so the bot sends the confirmation to the server owner by DM, falling back to the server's system channel.

Privacy Policy: say that Discord login (`identify`, `guilds`) is used only to show the servers you manage and tie the payment to your Discord ID, and that the Discord token is discarded straight after.

## 3. `POST /api/checkout` — create the Polar checkout

Both ways end here. **A:** the body has the signed token `t`. **B:** the body has `guild_id` and the session cookie identifies the person.

```js
let guildId, userId, channelId, trialDays;
if (req.body.t) {                                     // way A
  const token = verifyToken(req.body.t, process.env.BILLING_LINK_SECRET);
  if (!token) return res.status(400).send("Link expired");
  ({ g: guildId, u: userId, c: channelId } = token); trialDays = token.d;
} else {                                              // way B
  const session = getSession(req);                    // your session lookup
  if (!session || !session.manageableGuildIds.includes(req.body.guild_id)) return res.status(403).send("Not allowed");
  guildId = req.body.guild_id; userId = session.userId;
  trialDays = await trialDaysFor(guildId);            // from pulsenotify_billing, see 2B step 4
}

const metadata = { guild_id: guildId };               // the bot finds the payment by this
if (userId) metadata.user_id = userId;                // who paid: thanked by the bot; may open the billing portal
if (channelId) metadata.channel_id = channelId;       // where the bot posts the confirmation (way A only)

const body = {
  products: [process.env.POLAR_PRODUCT_ID],
  metadata,
  allow_trial: trialDays > 0,
  ...(trialDays > 0 && { trial_interval: "day", trial_interval_count: trialDays }),
  success_url: `${process.env.SITE_URL}/thanks?checkout_id={CHECKOUT_ID}`,   // Polar fills in {CHECKOUT_ID}
  return_url: req.body.t ? `${process.env.SITE_URL}/subscribe?t=${encodeURIComponent(req.body.t)}` : `${process.env.SITE_URL}/subscribe`, // "back" button on Polar's page
};
const r = await fetch("https://api.polar.sh/v1/checkouts/", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.POLAR_WEB_TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
if (!r.ok) return res.status(502).send("Couldn't start the payment — please try again.");   // log r.status and the body, never the token
const checkout = await r.json();
res.redirect(303, checkout.url);
```

The `metadata` keys must be exactly `guild_id`, `user_id`, `channel_id`. Polar
copies them to the subscription, which is how the bot recognises it.
Rate-limit this endpoint per IP.

## 4. `GET /thanks?checkout_id=…` — the success / warning / error alert

**Never trust the URL** — anyone can type `?checkout_id=`. Ask Polar:

```js
const r = await fetch(`https://api.polar.sh/v1/checkouts/${encodeURIComponent(id)}`, {
  headers: { Authorization: `Bearer ${process.env.POLAR_WEB_TOKEN}` },
});
```

Send `Cache-Control: no-store` on this page. Map the result:

| What Polar says | Alert | Text |
|---|---|---|
| `status: "succeeded"` | ✅ **Success** | "Payment received. Your server will be switched on within about 2 minutes, and the bot confirms in your Discord channel." If `trial_end` is set: "Your card is saved — the first charge is on {trial_end date}." |
| `status: "confirmed"` | ⚠️ **Warning** | "Your payment is being processed. Don't pay again — check Discord in a few minutes." |
| `status: "open"`, or no `checkout_id` | ⚠️ **Warning** | "The payment wasn't completed. If you closed the checkout, run `/pulsenotify subscribe` again." |
| `status: "expired"` | ⚠️ **Warning** | "This checkout expired. Run `/pulsenotify subscribe` in Discord for a new link." |
| `status: "failed"` | ❌ **Error** | "The payment didn't go through and nothing was charged. Try another card from `/pulsenotify subscribe`, or contact support@…" |
| Polar 404 / other error / your backend failing | ❌ **Error** | "We couldn't confirm your payment. If you were charged, contact support@… with your email — your payment is safe." |

Use `metadata.guild_id` from the response if you want to show or check which server this was for.

### Optional: "activating your server…"

After a `succeeded` checkout the bot needs up to a minute to notice. The page can poll your backend every ~5 s for up to ~2 minutes and turn "Activating…" into "✅ Active". The backend reads one row (service key, server side only):

```
GET {SUPABASE_URL}/rest/v1/pulsenotify_billing?guild_id=eq.{guild_id}&select=card_status,paid_until,trial_ends_at
Headers: apikey: {SERVICE_KEY}, Authorization: Bearer {SERVICE_KEY}, Accept-Profile: pulsenotify
```

Active when `card_status` is `trialing` or `active` for this checkout's server. Only use the `guild_id` from a checkout Polar confirmed as `succeeded`, return only those fields, and rate-limit it. If it doesn't turn active after ~2 minutes, show a ⚠️ warning: "Your payment is confirmed; activation is taking longer than usual — it will switch on automatically, or contact support."

## 5. Optional: crypto on the same page

Same token, so the server is authenticated: create the NOWPayments payment with
`order_id = "<guild_id>-<anything unique>"` and insert the `pending` row into
`pulsenotify.pulsenotify_payments` as described in the README ("Payments
setup", NOWPayments). Show a ✅ / ⚠️ / ❌ alert in the same way.

## 6. Security checklist

- `BILLING_LINK_SECRET`, `POLAR_WEB_TOKEN` and the Supabase key live only in server-side environment variables — never in HTML/JS, never in git, never logged.
- HTTPS only. Verify the token on the server for every request.
- HTML-escape everything from the token (`n`), the URL, and Discord data (server and user names).
- Discord login: check `state`; keep the Discord token only for the two API calls, then revoke it; session cookies `HttpOnly; Secure; SameSite=Lax`; validate every posted server ID against the session's list.
- The Supabase service key can read everything — use it only for the narrow reads above, never expose its results beyond the fields shown, and never accept table names, filters or IDs from the browser.
- Return the same generic error whatever went wrong inside; log the detail.
- Rate-limit `/api/checkout`, the status endpoint and (if offered) the crypto endpoint.
- Test with Polar's sandbox (`sandbox.polar.sh`, base URL `https://sandbox-api.polar.sh`) before going live.
