"""Thin async client for the Paddle Billing API (card payments, recurring).

Flow: the bot creates a transaction for the monthly price with the server
ID in custom_data; Paddle returns a checkout URL; once the customer pays,
Paddle puts a subscription_id on the transaction, and that subscription
renews monthly by itself. The bot learns all of this by polling (no
webhook endpoint needed) — see billing/service.py.

Checkout URLs open on the seller's "default payment link" — a page on the
seller's own approved website that loads Paddle.js (Paddle dashboard →
Checkout → Checkout settings). See README "Payments setup".

Auth: `Authorization: Bearer <API key>`. Responses wrap the object in
{"data": …}.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

LIVE_URL = "https://api.paddle.com"
SANDBOX_URL = "https://sandbox-api.paddle.com"


def is_sandbox_key(api_key: str, configured_sandbox: bool) -> bool:
    """Paddle keys say which environment they belong to (`pdl_sdbx_apikey_…`
    / `pdl_live_apikey_…`); a key sent to the other environment is always
    rejected. Trust the key; fall back to PADDLE_SANDBOX for older keys."""
    if api_key.startswith("pdl_sdbx_"):
        return True
    if api_key.startswith("pdl_live_"):
        return False
    return configured_sandbox


class PaddleError(Exception):
    """A Paddle API call failed in a way worth showing the user."""


class PaddleClient:
    def __init__(
        self,
        api_key: str,
        *,
        sandbox: bool = False,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=SANDBOX_URL if sandbox else LIVE_URL,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_transaction(
        self,
        price_id: str,
        custom_data: Dict[str, str],
        *,
        trial_days: int = 0,
        amount_cents: Optional[int] = None,
    ) -> Dict[str, Any]:
        """A checkout for one unit of `price_id`. Returns the transaction;
        its checkout["url"] is the payment page.

        With `amount_cents` and/or `trial_days`, the checkout uses a copy of
        that price (same product and billing cycle) charging `amount_cents`
        USD — so the price can be changed from the bot's settings without
        touching Paddle — with a trial of `trial_days`: Paddle saves the
        card now and charges only when the trial ends, so cancelling before
        then costs nothing."""
        item: Dict[str, Any] = {"price_id": price_id, "quantity": 1}
        if trial_days > 0 or amount_cents is not None:
            price = await self.get_price(price_id)
            unit_price = (
                {"amount": str(amount_cents), "currency_code": "USD"} if amount_cents is not None else price["unit_price"]
            )
            item = {
                "quantity": 1,
                "price": {
                    "product_id": price["product_id"],
                    "description": price.get("description") or "PulseNotify subscription",
                    "name": price.get("name") or "PulseNotify",
                    "unit_price": unit_price,
                    "billing_cycle": price["billing_cycle"],
                    "tax_mode": price.get("tax_mode") or "account_setting",
                },
            }
            if trial_days > 0:
                item["price"]["trial_period"] = {"interval": "day", "frequency": trial_days}
        return await self._request(
            "POST",
            "/transactions",
            json={"items": [item], "collection_mode": "automatic", "custom_data": custom_data},
        )

    async def get_price(self, price_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/prices/{price_id}")

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/transactions/{transaction_id}")

    async def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/subscriptions/{subscription_id}")

    async def create_portal_session(self, customer_id: str, subscription_ids: List[str]) -> Dict[str, Any]:
        """A one-off link to Paddle's customer portal, where the customer can
        update their card or cancel."""
        return await self._request(
            "POST", f"/customers/{customer_id}/portal-sessions", json={"subscription_ids": subscription_ids}
        )

    async def _request(self, method: str, path: str, *, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"Paddle request failed: {path}: {exc}") from exc

        if 200 <= response.status_code < 300:
            return response.json().get("data", {})

        detail = ""
        try:
            error = response.json().get("error", {})
            detail = error.get("detail") or error.get("code") or ""
        except ValueError:
            detail = response.text[:200]
        if response.status_code == 429:
            raise RateLimitedError(f"Paddle rate limited: {detail}", retry_after=60)
        if response.status_code in (401, 403):
            raise PaddleError(f"Paddle rejected the API key ({response.status_code}): {detail}")
        if response.status_code in (400, 404, 409, 422):
            raise PermanentPlatformError(f"Paddle {response.status_code}: {detail}")
        raise TransientPlatformError(f"Paddle error {response.status_code}: {detail}")
