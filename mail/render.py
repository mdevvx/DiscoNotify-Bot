"""Turns an email template plus its variables into the subject, HTML and
plain-text bodies that are sent. Pure functions.

Templates are edited in the admin panel and use `{{name}}` placeholders:
- in the subject and text body the value is inserted as is;
- in the HTML body it is HTML-escaped, so a server called `<b>x</b>` can't
  inject markup;
- `{{{name}}}` inserts a value unescaped, but only for the link variables
  (`URL_VARIABLES`) — any other name is escaped regardless.
A name that isn't known is replaced by nothing (and returned in `missing`,
so the admin panel's preview can flag it).

The HTML body is wrapped in a shared layout (logo, footer, unsubscribe
link for marketing emails) that the admin panel edits too.
"""

from __future__ import annotations

import datetime
import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

# Every variable a template may use, with what it holds. The admin panel
# shows this list next to the editor.
VARIABLES: Dict[str, str] = {
    "server_name": "The Discord server's name",
    "price": "The monthly price, e.g. $1.99",
    "trial_days": "Length of the free trial in days",
    "trial_ends_at": "When the free trial ends, e.g. October 29, 2026",
    "paid_until": "Until when the subscription is paid, e.g. November 29, 2026",
    "next_charge_date": "When the next card payment is taken",
    "status_line": "A ready-made sentence about the subscription's state",
    "subscribe_url": "Link to subscribe on the website",
    "activate_url": "Link to activate the free trial on the website",
    "manage_url": "Link to manage or cancel the subscription on the website",
    "unsubscribe_url": "Link to stop reminder emails (always set for marketing emails)",
    "support_email": "The support address",
    "reminder_number": "Which reminder this is (1, 2, 3)",
    "reminder_total": "How many reminders are sent in total",
    "brand_name": "The product name (DiscoNotify)",
}
URL_VARIABLES = frozenset({"subscribe_url", "activate_url", "manage_url", "unsubscribe_url"})

_PLACEHOLDER = re.compile(r"\{\{\{\s*(\w+)\s*\}\}\}|\{\{\s*(\w+)\s*\}\}")

DEFAULT_LAYOUT: Dict[str, Any] = {
    "brand_name": "DiscoNotify",
    "logo_url": "",
    "accent_color": "#5B6CF0",
    "footer_html": "This is an automated message from DiscoNotify — replies aren't monitored. Need help? Write to {{support_email}}.",
    "footer_text": "This is an automated message from DiscoNotify - replies aren't monitored. Need help? Write to {{support_email}}.",
}


@dataclass(frozen=True)
class Rendered:
    subject: str
    html: str
    text: str
    missing: List[str] = field(default_factory=list)


def format_date(moment: Optional[Any]) -> str:
    """October 29, 2026 (UTC). Accepts a datetime or an ISO string."""
    if moment is None or moment == "":
        return ""
    if not isinstance(moment, datetime.datetime):
        try:
            moment = datetime.datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
        except ValueError:
            return str(moment)
    return f"{moment.strftime('%B')} {moment.day}, {moment.year}"


def substitute(text: str, variables: Mapping[str, Any], *, escape: bool, missing: List[str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        raw_name, name = match.group(1), match.group(2)
        key = raw_name or name
        if key not in VARIABLES:
            if key not in missing:
                missing.append(key)
            return ""
        value = variables.get(key)
        value = "" if value is None else str(value)
        if not escape:
            return value
        if raw_name and key in URL_VARIABLES:
            return value
        return html.escape(value)

    return _PLACEHOLDER.sub(replace, text)


def render(template: Mapping[str, Any], variables: Mapping[str, Any], layout: Optional[Mapping[str, Any]] = None) -> Rendered:
    """`template` has subject, preheader, body_html, body_text (a database row
    or a manual email's custom content)."""
    layout = {**DEFAULT_LAYOUT, **(layout or {})}
    variables = {"brand_name": layout["brand_name"], **variables}
    missing: List[str] = []

    subject = substitute(str(template.get("subject") or ""), variables, escape=False, missing=missing)
    subject = " ".join(subject.split())  # a subject is one line
    preheader = substitute(str(template.get("preheader") or ""), variables, escape=False, missing=missing)
    body_html = substitute(str(template.get("body_html") or ""), variables, escape=True, missing=missing)
    body_text = substitute(str(template.get("body_text") or ""), variables, escape=False, missing=missing)

    unsubscribe_url = variables.get("unsubscribe_url") or ""
    footer_html = substitute(str(layout["footer_html"]), variables, escape=True, missing=missing)
    footer_text = substitute(str(layout["footer_text"]), variables, escape=False, missing=missing)
    if unsubscribe_url:
        link = html.escape(str(unsubscribe_url), quote=True)
        footer_html += f'<br><a href="{link}" style="color:#888">Unsubscribe from reminder emails</a>'
        footer_text += f"\n\nUnsubscribe from reminder emails: {unsubscribe_url}"

    return Rendered(
        subject=subject,
        html=_wrap_html(layout, preheader, body_html, footer_html),
        text=f"{body_text.strip()}\n\n--\n{footer_text}".strip(),
        missing=missing,
    )


def _wrap_html(layout: Mapping[str, Any], preheader: str, body_html: str, footer_html: str) -> str:
    accent = html.escape(str(layout["accent_color"]), quote=True)
    brand = html.escape(str(layout["brand_name"]))
    logo_url = str(layout.get("logo_url") or "")
    if logo_url:
        header = f'<img src="{html.escape(logo_url, quote=True)}" alt="{brand}" height="32" style="display:block;border:0">'
    else:
        header = f'<span style="font-size:18px;font-weight:700;color:{accent}">{brand}</span>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2430">'
        f'<span style="display:none;max-height:0;overflow:hidden;opacity:0">{html.escape(preheader)}</span>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;width:100%;background:#ffffff;border-radius:10px">'
        f'<tr><td style="padding:24px 32px 0">{header}</td></tr>'
        f'<tr><td style="padding:16px 32px 24px;font-size:15px;line-height:1.6">{body_html}</td></tr>'
        '<tr><td style="padding:16px 32px 24px;border-top:1px solid #eceef2;'
        f'font-size:12px;line-height:1.5;color:#888">{footer_html}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
