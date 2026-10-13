"""Who may use PulseNotify right now — pure logic over one billing row.

A server has access while ANY of these holds:
- it's whitelisted by a bot owner (never pays),
- it's inside its free trial,
- it's paid up (card subscription or crypto prepayment), plus a grace
  period after the paid time runs out, so a card renewal that fails once
  and succeeds on Paddle's retry doesn't cut monitoring in between.
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


def describe_access(access: Access, row: Optional[Dict[str, Any]], *, price_label: str = "$2.99") -> str:
    """One line for /pulsenotify status and /pulsenotify subscribe."""
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
    return "🔴 Subscription ended — monitoring is paused. Use `/pulsenotify subscribe`."


def _when(moment: Optional[datetime.datetime], style: str) -> str:
    """<t:…> — Discord renders it in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:{style}>" if moment else "soon"
