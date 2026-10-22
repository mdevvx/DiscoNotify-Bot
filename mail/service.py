"""Billing and reminder emails, sent from your own domain (mail/smtp.py).

Everything goes through the outbox (`notify_email_outbox`): the bot, the
website (trial activation) and the admin panel (manual sends) add rows, and
`process_outbox` — run by the bot's billing loop about once a minute — sends
what's due through the SMTP mailbox and records the result. So there is exactly one
place that talks to the email provider, every email is logged, and an
automatic email is never queued twice (each has a dedupe key).

Automatic emails (templates are edited in the admin panel):
- `trial_activated`        — queued by the website's activation (database function)
- `subscription_started`   — a payment or card subscription began
- `subscription_cancelled` — a card subscription was cancelled
- `payment_failed`         — a card payment failed (it's retried automatically)
- `renewal_reminder`       — a few days before the next card payment
- `subscription_renewed`   — a card payment for the next month went through
- `subscription_ended`     — a card subscription ended; alerts are paused
- `trial_ended_reminder`   — after a free trial ends: right away, then weekly, at most 3
- `subscription_lapsed_reminder` — the same series after a paid subscription runs out

Billing emails are transactional and always go out; the trial-ended
reminder is marketing, so it carries an unsubscribe link and is skipped for
anyone who unsubscribed. With no SMTP settings nothing is sent and rows stay
queued.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Mapping, Optional

from billing.access import Access, utcnow
from billing.links import sign_unsubscribe
from billing.service import BillingEvent, BillingService
from database.repositories.bot_state import BotStateRepository
from mail.render import DEFAULT_LAYOUT, format_date, render
from mail.repository import MailRepository
from mail.errors import MailError, PermanentMailError
from mail.smtp import SmtpClient
from utils.logger import get_logger

logger = get_logger(__name__)

LAYOUT_KEY = "email_layout"
SCHEDULE_KEY = "email_schedule"
_MAX_ATTEMPTS = 3
_RETRY_DELAY = datetime.timedelta(minutes=10)
_DATE_VARIABLES = ("trial_ends_at", "paid_until", "next_charge_date")
# billing event kind -> template (events not listed here send no email).
_EVENT_TEMPLATES = {
    "card_failed": "payment_failed",
    "card_cancel_scheduled": "subscription_cancelled",
    "card_renewed": "subscription_renewed",
    "card_ended": "subscription_ended",
}


class EmailService:
    def __init__(
        self,
        repo: MailRepository,
        bot_state: BotStateRepository,
        *,
        client: Optional[SmtpClient] = None,
        link_secret: Optional[str] = None,
        unsubscribe_url: Optional[str] = None,
        subscribe_url: str = "",
        activate_url: str = "",
        manage_url: str = "",
        support_email: str = "",
        price_label: str = "",
        trial_days: int = 7,
        reminder_every_days: int = 7,
        reminder_max: int = 3,
        renewal_days: int = 3,
        clock: Any = utcnow,
    ) -> None:
        self._repo = repo
        self._bot_state = bot_state
        self._client = client
        self._link_secret = link_secret or None
        self._unsubscribe_url = unsubscribe_url or None
        self._subscribe_url = subscribe_url
        self._activate_url = activate_url
        self._manage_url = manage_url
        self._support_email = support_email
        self._price_label = price_label
        self._trial_days = trial_days
        self._reminder_every_days = reminder_every_days
        self._reminder_max = reminder_max
        self._renewal_days = renewal_days
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── settings the admin panel edits ──────────────────────────────────

    async def layout(self) -> Dict[str, Any]:
        return {**DEFAULT_LAYOUT, **(await self._setting(LAYOUT_KEY))}

    async def schedule(self) -> Dict[str, int]:
        """renewal_days_before, trial_reminder_every_days, trial_reminder_max —
        the admin panel's values, else the bot's configuration."""
        stored = await self._setting(SCHEDULE_KEY)
        defaults = {
            "renewal_days_before": self._renewal_days,
            "trial_reminder_every_days": self._reminder_every_days,
            "trial_reminder_max": self._reminder_max,
        }
        result = dict(defaults)
        for key, fallback in defaults.items():
            value = stored.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        return result

    async def _setting(self, key: str) -> Dict[str, Any]:
        try:
            value = await self._bot_state.get(key)
        except Exception:
            logger.warning("Couldn't read the %s email setting.", key, exc_info=True)
            return {}
        return value if isinstance(value, dict) else {}

    # ── queueing ────────────────────────────────────────────────────────

    async def queue(self, template_key: str, guild_id: int, variables: Mapping[str, Any], *, dedupe_key: str) -> int:
        """Queues the template for every contact of the server; how many were
        queued. Nothing if the template is off or missing, the server has no
        contacts, or (for a marketing template) the person unsubscribed."""
        template = await self._repo.get_template(template_key)
        if template is None or not template.get("enabled"):
            return 0
        marketing = template.get("category") == "marketing"
        queued = 0
        for contact in await self._repo.contacts_for_guild(guild_id):
            email = str(contact["email"]).lower()
            if marketing and await self._repo.is_unsubscribed(email):
                continue
            if await self._repo.enqueue(
                to_email=email,
                template_key=template_key,
                guild_id=guild_id,
                variables=_jsonable(variables),
                dedupe_key=f"{dedupe_key}:{email}",
            ):
                queued += 1
        return queued

    # ── sending ─────────────────────────────────────────────────────────

    async def process_outbox(self) -> int:
        """Sends the emails that are due; how many went out."""
        if self._client is None:
            return 0
        now = self._clock()
        sent = 0
        layout = await self.layout()
        for row in await self._repo.due(now):
            if not await self._repo.claim(row["id"]):
                continue
            try:
                if await self._send_row(row, layout, now):
                    sent += 1
            except Exception:
                logger.exception("Sending email %s failed unexpectedly.", row.get("id"))
                await self._repo.mark_failed(row["id"], "unexpected error", attempts=int(row.get("attempts") or 0) + 1)
        return sent

    async def _send_row(self, row: Dict[str, Any], layout: Dict[str, Any], now: datetime.datetime) -> bool:
        assert self._client is not None
        attempts = int(row.get("attempts") or 0) + 1
        to_email = str(row["to_email"]).lower()

        template: Optional[Mapping[str, Any]]
        category = "transactional"
        if row.get("template_key"):
            template = await self._repo.get_template(row["template_key"])
            if template is None:
                await self._repo.mark_skipped(row["id"], "template not found")
                return False
            if row.get("kind") == "auto" and not template.get("enabled"):
                await self._repo.mark_skipped(row["id"], "template is switched off")
                return False
            category = str(template.get("category") or "transactional")
            if row.get("subject_override"):
                template = {**template, "subject": row["subject_override"]}
            if row.get("body_html") or row.get("body_text"):
                template = {
                    **template,
                    "body_html": row.get("body_html") or template.get("body_html"),
                    "body_text": row.get("body_text") or template.get("body_text"),
                }
        else:
            if not (row.get("subject_override") and (row.get("body_html") or row.get("body_text"))):
                await self._repo.mark_skipped(row["id"], "no template and no custom content")
                return False
            template = {
                "subject": row["subject_override"],
                "body_html": row.get("body_html") or "",
                "body_text": row.get("body_text") or "",
            }
            category = "marketing" if (row.get("variables") or {}).get("marketing") else "transactional"

        if category == "marketing" and await self._repo.is_unsubscribed(to_email):
            await self._repo.mark_skipped(row["id"], "unsubscribed")
            return False

        variables = self._variables(row.get("variables") or {}, to_email, row.get("guild_id"), marketing=category == "marketing")
        rendered = render(template, variables, layout)
        if rendered.missing:
            logger.warning("Email %s uses unknown variable(s) %s.", row.get("id"), rendered.missing)
        try:
            provider_id = await self._client.send(
                to=to_email,
                subject=rendered.subject,
                html=rendered.html,
                text=rendered.text,
                unsubscribe_url=variables.get("unsubscribe_url") or None,
                idempotency_key=str(row["id"]),
            )
        except PermanentMailError as exc:
            logger.warning("Email %s was refused: %s", row.get("id"), exc)
            await self._repo.mark_failed(row["id"], str(exc), attempts=attempts)
            return False
        except MailError as exc:
            if attempts >= _MAX_ATTEMPTS:
                await self._repo.mark_failed(row["id"], str(exc), attempts=attempts)
            else:
                await self._repo.requeue(row["id"], str(exc), attempts=attempts, send_after=now + _RETRY_DELAY * attempts)
            return False
        await self._repo.mark_sent(row["id"], subject=rendered.subject, provider_id=provider_id, attempts=attempts, now=now)
        return True

    def _variables(self, stored: Mapping[str, Any], to_email: str, guild_id: Optional[int], *, marketing: bool) -> Dict[str, Any]:
        variables: Dict[str, Any] = {
            "server_name": "your server",
            "price": self._price_label,
            "trial_days": self._trial_days,
            "subscribe_url": self._subscribe_url,
            "activate_url": self._activate_url,
            "manage_url": self._manage_url,
            "support_email": self._support_email,
            "reminder_total": self._reminder_max,
        }
        variables.update({key: value for key, value in stored.items() if value is not None})
        for key in _DATE_VARIABLES:
            if key in variables:
                variables[key] = format_date(variables[key])
        if marketing and self._link_secret and self._unsubscribe_url:
            token = sign_unsubscribe(self._link_secret, email=to_email, guild_id=guild_id, now=self._clock().timestamp())
            separator = "&" if "?" in self._unsubscribe_url else "?"
            variables["unsubscribe_url"] = f"{self._unsubscribe_url}{separator}t={token}"
        return variables

    # ── automatic emails ────────────────────────────────────────────────

    async def on_payment(self, payment: Dict[str, Any], access: Access, row: Mapping[str, Any], server_name: str) -> int:
        """A payment or card subscription began."""
        guild_id = int(payment["guild_id"])
        if payment.get("provider") == "polar" and row.get("card_status") == "trialing":
            until = format_date(access.until)
            status_line = (
                f"Your free trial continues until {until}, and the first {self._price_label} charge is then."
                if until
                else "Your card is saved; nothing is charged until the free trial ends."
            )
        else:
            until = format_date(access.until)
            status_line = f"Your subscription is active until {until}." if until else "Your subscription is active."
        variables = {
            "server_name": server_name,
            "paid_until": access.until if access.until else None,
            "next_charge_date": access.until if access.until else None,
            "status_line": status_line,
        }
        return await self.queue("subscription_started", guild_id, variables, dedupe_key=f"subscription_started:{payment['id']}")

    async def on_billing_event(self, event: BillingEvent, server_name: str) -> int:
        template = _EVENT_TEMPLATES.get(event.kind)
        if template is None:
            return 0
        variables: Dict[str, Any] = {"server_name": server_name}
        if event.kind == "card_cancel_scheduled":
            variables["status_line"] = (
                f"Your access continues until {format_date(event.until)}." if event.until else ""
            )
            key = f"{event.kind}:{event.guild_id}:{format_date(event.until)}"
        elif event.kind == "card_renewed":
            variables["paid_until"] = event.until
            variables["status_line"] = f"Your subscription is active until {format_date(event.until)}." if event.until else ""
            key = f"{event.kind}:{event.guild_id}:{format_date(event.until)}"
        elif event.kind == "card_ended":
            variables["status_line"] = "Alerts are paused, and every setting is kept."
            key = f"{event.kind}:{event.guild_id}:{format_date(event.until or self._clock())}"
        else:
            key = f"{event.kind}:{event.guild_id}:{self._clock().date().isoformat()}"
        return await self.queue(template, event.guild_id, variables, dedupe_key=key)

    async def send_renewal_reminders(self, billing: BillingService, guild_names: Mapping[int, str]) -> int:
        """"Your payment is coming up", a few days before a card payment."""
        schedule = await self.schedule()
        days = schedule["renewal_days_before"]
        if days <= 0:
            return 0
        queued = 0
        for charge in await billing.upcoming_card_charges(datetime.timedelta(days=days)):
            guild_id = int(charge["guild_id"])
            name = guild_names.get(guild_id)
            if name is None:
                continue
            variables = {"server_name": name, "next_charge_date": charge["charge_at"]}
            queued += await self.queue(
                "renewal_reminder", guild_id, variables, dedupe_key=f"renewal:{guild_id}:{format_date(charge['charge_at'])}"
            )
        return queued

    async def send_lapse_reminders(self, billing: BillingService, guild_names: Mapping[int, str]) -> int:
        """The upgrade reminder after a free trial ended, or the renew
        reminder after a paid subscription ran out: right away, then every N
        days, at most M times (the admin panel's schedule, else the bot's
        configuration)."""
        schedule = await self.schedule()
        maximum = schedule["trial_reminder_max"]
        queued = 0
        for row in await billing.reminders_due(guild_names.keys(), every_days=schedule["trial_reminder_every_days"], maximum=maximum):
            guild_id = int(row["guild_id"])
            number = int(row.get("reminders_sent") or 0) + 1
            lapsed_subscription = bool(row.get("paid_until"))
            template = "subscription_lapsed_reminder" if lapsed_subscription else "trial_ended_reminder"
            ended_at = row.get("paid_until") if lapsed_subscription else row.get("trial_ends_at")
            variables = {
                "server_name": guild_names[guild_id],
                "trial_ends_at": row.get("trial_ends_at"),
                "paid_until": row.get("paid_until"),
                "reminder_number": number,
                "reminder_total": maximum,
            }
            contacts = await self._repo.contacts_for_guild(guild_id)
            if not contacts:
                continue  # nobody to email (a server that predates email collection); check again later
            queued += await self.queue(
                template,
                guild_id,
                variables,
                dedupe_key=f"{template}:{guild_id}:{format_date(ended_at)}:{number}",
            )
            # Counted even if everyone had unsubscribed, so the schedule moves on.
            await billing.mark_reminded(row)
        return queued


def _jsonable(variables: Mapping[str, Any]) -> Dict[str, Any]:
    """Stored as JSON: dates become ISO strings (formatted when the email is sent)."""
    return {
        key: value.isoformat() if isinstance(value, datetime.datetime) else value for key, value in variables.items()
    }
