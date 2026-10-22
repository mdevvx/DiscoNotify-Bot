"""Who may use DiscoNotify right now — pure logic over one billing row.

A server has access while ANY of these holds:
- it's whitelisted by a bot owner (never pays),
- it's inside its free trial,
- it's paid up (card subscription or crypto prepayment), plus a grace
  period after the paid time runs out, so a card renewal that fails once
  and succeeds on the payment provider's retry doesn't cut monitoring in between.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass
from typing import Any, Dict, Optional


class AccessReason(str, enum.Enum):
    BILLING_OFF = "billing_off"  # BILLING_ENABLED=false — everything is free
    WHITELISTED = "whitelisted"
    TRIAL = "trial"
    PAID = "paid"
    GRACE = "grace"
    EXPIRED = "expired"
    # A new server whose free trial hasn't been activated on the website yet.
    NEEDS_ACTIVATION = "needs_activation"


@dataclass(frozen=True)
class Access:
    allowed: bool
    reason: AccessReason
    # When the current state ends (trial end, paid-until, grace end); None
    # for whitelisted/billing-off/expired.
    until: Optional[datetime.datetime] = None


def evaluate(row: Dict[str, Any], *, now: datetime.datetime, grace: datetime.timedelta) -> Access:
    if row.get("whitelisted"):
        return Access(True, AccessReason.WHITELISTED)

    paid_until = parse_time(row.get("paid_until"))
    if paid_until is not None and now < paid_until:
        return Access(True, AccessReason.PAID, paid_until)

    trial_ends_at = parse_time(row.get("trial_ends_at"))
    if trial_ends_at is not None and now < trial_ends_at:
        return Access(True, AccessReason.TRIAL, trial_ends_at)

    if paid_until is not None and now < paid_until + grace:
        return Access(True, AccessReason.GRACE, paid_until + grace)

    if row.get("activation_required") and trial_ends_at is None and paid_until is None:  # never paid either
        return Access(False, AccessReason.NEEDS_ACTIVATION)

    return Access(False, AccessReason.EXPIRED)


def parse_time(value: Any) -> Optional[datetime.datetime]:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        moment = value
    else:
        try:
            moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def describe_access(access: Access, row: Optional[Dict[str, Any]], *, price_label: str = "$1.99") -> str:
    """One line for /notify status and /notify subscribe."""
    row = row or {}
    if access.reason == AccessReason.BILLING_OFF:
        return "🎁 Free — no subscription needed"
    if access.reason == AccessReason.WHITELISTED:
        return "⭐ Whitelisted by the bot owner — no payment needed"
    if access.reason == AccessReason.TRIAL:
        if row.get("card_status") == "trialing" and not row.get("card_cancels_at"):
            return f"🎁 Free trial — card subscription starts {_when(access.until, 'R')} ({price_label}/month)"
        return f"🎁 Free trial — ends {_when(access.until, 'R')}"
    if access.reason == AccessReason.PAID:
        renews = row.get("card_status") == "active" and not row.get("card_cancels_at")
        how = "renews automatically by card" if renews else "then it stops unless renewed"
        return f"✅ Paid until {_when(access.until, 'D')} — {how}"
    if access.reason == AccessReason.GRACE:
        return f"⚠️ Payment overdue — monitoring stops {_when(access.until, 'R')} unless renewed"
    if access.reason == AccessReason.NEEDS_ACTIVATION:
        return "🕐 Free trial not started yet — activate it on the website to begin"
    return "🔴 Subscription ended — monitoring is paused. Use `/notify subscribe`."


def _when(moment: Optional[datetime.datetime], style: str) -> str:
    """<t:…> — Discord renders it in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "soon"


def activation_needed(url: str) -> "list[str]":
    """What a new server sees until its free trial is activated."""
    return [
        "This server's free trial hasn't been started yet. Activate it on the website — it takes a minute, "
        "and the trial begins the moment you do.",
        f"**{url}**",
        "Log in with Discord, pick this server, and you're set. A server admin (Manage Server) can do it.",
    ]


def subscription_needed(row: Optional[Dict[str, Any]], *, price_label: str) -> "list[str]":
    """What a server without access sees when it runs a command: what ended
    and when, that nothing is lost, and exactly how to get it back."""
    row = row or {}
    paid_until = parse_time(row.get("paid_until"))
    trial_ends_at = parse_time(row.get("trial_ends_at"))
    if paid_until is not None:
        ended = f"This server's DiscoNotify subscription ended on {_when(paid_until, 'D')}."
    elif trial_ends_at is not None:
        ended = f"This server's free trial ended on {_when(trial_ends_at, 'D')}."
    else:
        ended = "This server doesn't have an active DiscoNotify subscription."
    return [
        ended,
        "Alerts are paused, but **every setting is kept** — nothing needs setting up again.",
        f"To switch it back on, a server admin (Manage Server) runs **`/notify subscribe`** — "
        f"**{price_label}/month**, card or crypto. Alerts resume within about 2 minutes of paying.",
    ]


def subscription_details(access: Access, row: Optional[Dict[str, Any]], *, price_label: str) -> "list[str]":
    """The Subscription section of /notify status: state, how it's
    paid, and where to manage it."""
    row = row or {}
    lines = [describe_access(access, row, price_label=price_label)]
    if access.reason in (AccessReason.WHITELISTED, AccessReason.BILLING_OFF):
        return lines

    card_status = row.get("card_status")
    cancels_at = parse_time(row.get("card_cancels_at"))
    if card_status in ("active", "trialing", "past_due"):
        if cancels_at is not None:
            lines.append(f"💳 Card subscription cancelled — no further charges; ends {_when(cancels_at, 'D')}")
        elif card_status == "trialing":
            lines.append(f"💳 Card saved — first {price_label} charge when the trial ends")
        elif card_status == "past_due":
            lines.append("💳 Card payment failed — it will be retried; update the card with `/notify cancel` to keep alerts running")
        else:
            lines.append(f"💳 Card subscription — {price_label}/month, renews automatically")
    elif access.reason in (AccessReason.PAID, AccessReason.GRACE):
        lines.append("🪙 Prepaid with crypto — renew before it ends to keep alerts running")

    if access.allowed:
        if card_status in ("active", "trialing", "past_due") and cancels_at is None:
            lines.append("Cancel any time: `/notify cancel`")
        else:
            lines.append("Subscribe or renew: `/notify subscribe`")
    else:
        lines.append(f"Turn alerts back on: `/notify subscribe` — {price_label}/month, card or crypto")
    return lines
