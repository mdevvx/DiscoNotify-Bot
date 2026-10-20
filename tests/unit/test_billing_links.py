"""Signed links from the bot to the website's subscribe page: genuine,
tamper-proof, expiring — and the service that builds them."""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any, Dict
from urllib.parse import parse_qs, urlsplit

import pytest

from billing.links import sign_link, verify_link
from billing.repository import BillingRepository
from billing.service import BillingService
from tests.unit.fakes import FakeDatabase

_SECRET = "a-long-random-shared-secret"
_NOW = 1_792_000_000.0


def _token(**kwargs: Any) -> str:
    defaults: Dict[str, Any] = {"guild_id": 1466009969806545009, "user_id": 947795908119109652, "now": _NOW}
    defaults.update(kwargs)
    return sign_link(_SECRET, **defaults)


def test_a_genuine_token_carries_the_server_the_requester_and_the_trial() -> None:
    token = _token(channel_id=555, guild_name="Custom Bots Development", trial_days=5)

    payload = verify_link(_SECRET, token, now=_NOW + 60)

    assert payload == {
        "v": 1,
        "g": "1466009969806545009",  # IDs are strings: they don't fit a JavaScript number
        "u": "947795908119109652",
        "c": "555",
        "n": "Custom Bots Development",
        "d": 5,
        "e": int(_NOW) + 1800,
    }


def test_optional_fields_are_left_out() -> None:
    payload = verify_link(_SECRET, sign_link(_SECRET, guild_id=7, now=_NOW), now=_NOW)

    assert payload == {"v": 1, "g": "7", "d": 0, "e": int(_NOW) + 1800}


def test_a_token_expires() -> None:
    token = _token(ttl_seconds=600)

    assert verify_link(_SECRET, token, now=_NOW + 599) is not None
    assert verify_link(_SECRET, token, now=_NOW + 601) is None


def test_a_different_secret_or_a_tampered_payload_is_refused() -> None:
    token = _token()
    payload_part, signature = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload_part + "=="))
    forged["g"] = "42"  # someone else's server
    forged_part = base64.urlsafe_b64encode(json.dumps(forged, separators=(",", ":")).encode()).rstrip(b"=").decode()

    assert verify_link("another-secret", token, now=_NOW) is None
    assert verify_link(_SECRET, f"{forged_part}.{signature}", now=_NOW) is None


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", ".", "not-base64.not-base64", "e30.e30"])
def test_malformed_tokens_are_refused_not_crashed_on(junk: str) -> None:
    assert verify_link(_SECRET, junk, now=_NOW) is None


async def test_the_service_builds_the_signed_website_link_with_the_remaining_trial() -> None:
    db = FakeDatabase()
    db.tables["notify_guilds"].rows.append({"guild_id": 1, "enabled": True})
    clock_now = datetime.datetime.fromtimestamp(_NOW, datetime.timezone.utc)
    service = BillingService(
        BillingRepository(db),  # type: ignore[arg-type]
        enabled=True,
        subscribe_url="https://disconotify.com/subscribe?ref=bot",
        link_secret=_SECRET,
        clock=lambda: clock_now,
    )
    await service.access(1)  # starts the 7-day trial

    url = await service.website_subscribe_url(1, user_id=9, channel_id=5, guild_name="Hub")

    parts = urlsplit(url)
    assert service.website_mode is True
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "disconotify.com", "/subscribe")
    query = parse_qs(parts.query)
    assert query["ref"] == ["bot"]
    payload = verify_link(_SECRET, query["t"][0], now=_NOW)
    assert payload is not None and (payload["g"], payload["u"], payload["c"], payload["n"], payload["d"]) == ("1", "9", "5", "Hub", 7)


async def test_without_the_secret_or_the_url_the_bot_keeps_creating_checkouts_itself() -> None:
    db = FakeDatabase()
    only_url = BillingService(BillingRepository(db), enabled=True, subscribe_url="https://x.org/subscribe")  # type: ignore[arg-type]
    only_secret = BillingService(BillingRepository(db), enabled=True, link_secret=_SECRET)  # type: ignore[arg-type]

    assert only_url.website_mode is False and await only_url.website_subscribe_url(1) is None
    assert only_secret.website_mode is False and await only_secret.website_subscribe_url(1) is None
