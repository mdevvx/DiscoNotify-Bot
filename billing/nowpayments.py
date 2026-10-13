"""Thin async client for the NOWPayments API (crypto, prepaid months).

Uses the direct payment API rather than hosted invoices: creating a
payment returns the exact amount and deposit address to show in Discord,
and checking it by ID needs only the API key (`x-api-key`). Matching a
hosted invoice back to its payment needs an extra login (JWT) according to
part of NOWPayments' docs, so it's avoided (researched 2026-10-13).

Statuses: waiting, confirming, confirmed, sending, partially_paid,
finished, failed, refunded, expired. "confirmed" means the blockchain has
confirmed the funds; "sending"/"finished" follow once NOWPayments forwards
them to the merchant wallet.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

LIVE_URL = "https://api.nowpayments.io/v1"
SANDBOX_URL = "https://api-sandbox.nowpayments.io/v1"

PAID_STATUSES = frozenset({"confirmed", "sending", "finished"})
FAILED_STATUSES = frozenset({"failed", "refunded"})
EXPIRED_STATUSES = frozenset({"expired"})


class NowPaymentsClient:
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
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_payment(
        self, *, price_usd: float, pay_currency: str, order_id: str, description: str
    ) -> Dict[str, Any]:
        """Returns payment_id, pay_address, pay_amount, pay_currency and,
        for coins that need one, payin_extra_id (memo/tag)."""
        return await self._request(
            "POST",
            "/payment",
            json={
                "price_amount": price_usd,
                "price_currency": "usd",
                "pay_currency": pay_currency,
                "order_id": order_id,
                "order_description": description,
            },
        )

    async def min_amount_usd(self, pay_currency: str) -> float:
        """The smallest payment NOWPayments accepts in this coin right now,
        in USD. It moves with network fees — e.g. on 2026-10-13 USDT on
        TRON needed ~$11.18 and BTC ~$21.98, while USDT on BNB Chain, LTC,
        SOL and ETH needed under $1."""
        data = await self._request(
            "GET",
            "/min-amount",
            params={"currency_from": pay_currency, "currency_to": pay_currency, "fiat_equivalent": "usd"},
        )
        return float(data.get("fiat_equivalent") or 0)

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/payment/{payment_id}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"NOWPayments request failed: {path}: {exc}") from exc

        if 200 <= response.status_code < 300:
            return response.json()

        message = response.text[:200]
        try:
            message = response.json().get("message") or message
        except ValueError:
            pass
        if response.status_code == 429:
            raise RateLimitedError(f"NOWPayments rate limited: {message}", retry_after=60)
        if 400 <= response.status_code < 500:
            # e.g. "amountTo is too small" — below the coin's minimum.
            raise PermanentPlatformError(f"NOWPayments: {message}")
        raise TransientPlatformError(f"NOWPayments error {response.status_code}: {message}")
