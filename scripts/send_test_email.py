"""Sends a test email through the bot's own email settings, to check that the
SMTP login works and that the message arrives (and lands in the inbox, not spam).

    python scripts/send_test_email.py you@gmail.com
        Sends a sample email straight through the SMTP server (no database, no bot).
        Prints exactly why it failed if it did.

    python scripts/send_test_email.py you@gmail.com --template payment_failed
        Renders a real template from the database with sample data and sends it.

    python scripts/send_test_email.py you@gmail.com --template payment_failed --queue
        Puts the email in the outbox instead, so the running bot sends it within
        about a minute. This tests the whole path: outbox -> bot -> SMTP.

Uses the same settings as the bot (.env). Nothing is sent to anyone but the address you give.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from mail.errors import MailError  # noqa: E402
from mail.render import DEFAULT_LAYOUT, render  # noqa: E402
from mail.smtp import SmtpClient  # noqa: E402

_SAMPLE_TEMPLATE = {
    "subject": "DiscoNotify test email",
    "preheader": "If you can read this, sending works.",
    "body_html": "<h1>It works</h1><p>This is a test email from <strong>{{server_name}}</strong>'s DiscoNotify setup.</p>"
    '<p><a href="{{{subscribe_url}}}">A sample link</a></p>',
    "body_text": "It works\n\nThis is a test email from {{server_name}}'s DiscoNotify setup.\n\nA sample link: {{subscribe_url}}",
}


def _sample_variables(settings: Any) -> Dict[str, Any]:
    in_a_week = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    site = (settings.billing_subscribe_url or "https://disconotify.com/subscribe").rsplit("/", 1)[0]
    return {
        "server_name": "Test Server",
        "price": f"${settings.billing_price_usd:.2f}",
        "trial_days": settings.billing_trial_days,
        "trial_ends_at": in_a_week.isoformat(),
        "paid_until": in_a_week.isoformat(),
        "next_charge_date": in_a_week.isoformat(),
        "status_line": "Your subscription is active until the date above.",
        "subscribe_url": settings.billing_subscribe_url or f"{site}/subscribe",
        "activate_url": f"{site}/activate",
        "manage_url": f"{site}/manage",
        "support_email": settings.support_email or "support@disconotify.com",
        "reminder_number": 1,
        "reminder_total": settings.billing_reminder_max,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Send a test email with the bot's email settings.")
    parser.add_argument("to", help="where to send it (your own address)")
    parser.add_argument("--template", help="a template key from the database (e.g. payment_failed)")
    parser.add_argument("--queue", action="store_true", help="queue it in the outbox for the running bot instead of sending directly")
    args = parser.parse_args()

    settings = get_settings()
    variables = _sample_variables(settings)
    template: Dict[str, Any] = dict(_SAMPLE_TEMPLATE)
    category = "transactional"

    database = None
    if args.template or args.queue:
        from database.client import Database
        from mail.repository import MailRepository

        database = Database(settings.supabase_url, settings.supabase_key)
        await database.connect()
        repo = MailRepository(database)
        if args.template:
            row = await repo.get_template(args.template)
            if row is None:
                print(f"No template called {args.template!r} in the database. Did you run migrations 0013 and 0014?")
                return 1
            template, category = row, str(row.get("category") or "transactional")
        if args.queue:
            if not args.template:
                print("--queue needs --template.")
                return 1
            await database.client.table("notify_email_outbox").insert(
                {
                    "to_email": args.to.lower(),
                    "template_key": args.template,
                    "guild_id": None,
                    "kind": "manual",
                    "variables": {k: v for k, v in variables.items() if k != "unsubscribe_url"},
                    "subject_override": f"[TEST] {template['subject']}",
                    "created_by": "send_test_email.py",
                }
            ).execute()
            print(f"Queued. The running bot sends it within about a minute - check {args.to}, then the outbox row's status.")
            return 0

    if not settings.email_configured:
        print("Email isn't configured. Set EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD and EMAIL_FROM in .env.")
        return 1

    if category == "marketing":
        variables["unsubscribe_url"] = (settings.email_unsubscribe_url or "https://disconotify.com/unsubscribe") + "?t=test"
    rendered = render(template, variables, DEFAULT_LAYOUT)
    if rendered.missing:
        print(f"Warning: the template uses unknown variable(s): {', '.join(rendered.missing)}")

    client = SmtpClient(
        settings.email_smtp_host,  # type: ignore[arg-type]
        settings.email_smtp_port,
        settings.email_smtp_username,  # type: ignore[arg-type]
        settings.email_smtp_password.get_secret_value(),  # type: ignore[union-attr]
        sender=settings.email_from,  # type: ignore[arg-type]
        security=settings.email_smtp_mode,
    )
    print(
        f"Sending from {settings.email_from} via {settings.email_smtp_host}:{settings.email_smtp_port} "
        f"({settings.email_smtp_mode}) to {args.to} ..."
    )
    try:
        message_id = await client.send(
            to=args.to,
            subject=f"[TEST] {rendered.subject}",
            html=rendered.html,
            text=rendered.text,
            unsubscribe_url=variables.get("unsubscribe_url"),
        )
    except MailError as exc:
        print(f"FAILED ({type(exc).__name__}): {exc}")
        return 1
    finally:
        if database is not None:
            await database.close()
    print(f"Accepted by the mail server. Message-ID: {message_id}")
    print("Now check the inbox AND the spam folder, then in Gmail: the three dots, then Show original: SPF, DKIM and DMARC should all say PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
