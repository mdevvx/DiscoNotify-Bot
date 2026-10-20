"""Signed links from the bot to the website's subscribe page.

`/notify subscribe` gives a button to `BILLING_SUBSCRIBE_URL?t=<token>`.
The token carries what Discord already authenticated — which server, who
asked, in which channel, how many trial days are left — signed with a secret
shared between the bot and the website (BILLING_LINK_SECRET), and it expires.
So the website needs no Discord login, nobody can forge a server ID, and the
website can create the Polar checkout with the right metadata and trial.

Format (the website re-implements this; see docs/website-integration.md):

    token   = <payload> "." <signature>
    payload = base64url(compact JSON), no padding
    signature = base64url(HMAC-SHA256(secret, payload)), no padding

JSON fields: v (1), g (server ID), u (user ID, optional), c (channel ID,
optional), n (server name, optional, for display), d (trial days left, 0 if
none), e (expiry, unix seconds). IDs are strings (they exceed JavaScript's
safe integer range).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

_VERSION = 1
_MAX_NAME_LENGTH = 80


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_link(
    secret: str,
    *,
    guild_id: int,
    user_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    guild_name: Optional[str] = None,
    trial_days: int = 0,
    ttl_seconds: int = 1800,
    now: Optional[float] = None,
) -> str:
    payload: Dict[str, Any] = {
        "v": _VERSION,
        "g": str(guild_id),
        "d": max(0, int(trial_days)),
        "e": int((time.time() if now is None else now) + ttl_seconds),
    }
    if user_id:
        payload["u"] = str(user_id)
    if channel_id:
        payload["c"] = str(channel_id)
    if guild_name:
        payload["n"] = guild_name[:_MAX_NAME_LENGTH]
    encoded = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return f"{encoded}.{_sign(secret, encoded)}"


def verify_link(secret: str, token: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The payload if the token is genuine and unexpired, else None."""
    try:
        encoded, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(secret, encoded)):
            return None
        payload = json.loads(_unb64(encoded))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        return None
    if int(payload.get("e", 0)) < (time.time() if now is None else now):
        return None
    return payload


def _sign(secret: str, encoded_payload: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest())
