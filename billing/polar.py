"""Thin async client for the Polar API (polar.sh) — card subscriptions.

Polar is a merchant of record: it charges the customer, handles sales tax
and pays the seller out. The flow:
- `/pulsenotify subscribe` creates a Checkout Session for the server
  (POST /checkouts/) with metadata.guild_id and a trial of the days the
  server has left. Polar hosts the payment page;
- once it's paid, the subscription carries that metadata, and the bot finds
  it by listing subscriptions (no webhook endpoint needed — see
  billing/service.py);
- cancelling and changing the card happen in Polar's hosted customer
  portal, reached through a pre-authenticated link
  (POST /customer-sessions/).

Auth: `Authorization: Bearer <organization access token>` (Polar dashboard →
Settings → Developers → New token) with the scopes `checkouts:write`,
`subscriptions:read` and `customer_sessions:write` (`products:read` is
optional — it only lets the startup check compare the price).

Production is https://api.polar.sh/v1, sandbox https://sandbox-api.polar.sh/v1
(a separate account at sandbox.polar.sh).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Sequence

import httpx

from utils.errors import PermanentPlatformError, RateLimitedError, TransientPlatformError

LIVE_URL = "https://api.polar.sh/v1"
SANDBOX_URL = "https://sandbox-api.polar.sh/v1"
_PAGE_SIZE = 100
_MAX_PAGES = 20
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0


class PolarError(Exception):
    """A Polar API call failed in a way worth showing the user (a bad or
    under-scoped access token)."""


class PolarClient:
    def __init__(
        self,
        access_token: str,
        *,
        sandbox: bool = False,
        timeout_seconds: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """`transport` is for tests (httpx.MockTransport) only."""
        self._http = httpx.AsyncClient(
            base_url=SANDBOX_URL if sandbox else LIVE_URL,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_checkout(
        self,
        product_id: str,
        *,
        metadata: Dict[str, str],
        trial_days: int = 0,
        success_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """A hosted checkout for `product_id`; its `url` is the payment page.
        With `trial_days` the card is saved now and charged when the trial
        ends (cancel before then and nothing is charged); without, the
        product's own trial — none — applies and the first charge is
        immediate. `metadata` is copied to the subscription."""
        body: Dict[str, Any] = {"products": [product_id], "metadata": metadata, "allow_trial": trial_days > 0}
        if trial_days > 0:
            body["trial_interval"] = "day"
            body["trial_interval_count"] = min(trial_days, 1000)
        if success_url:
            body["success_url"] = success_url
        return await self._request("POST", "/checkouts/", json=body)

    async def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/subscriptions/{subscription_id}")

    async def list_subscriptions(
        self,
        *,
        started_after: Optional[datetime.datetime] = None,
        statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first. With no `started_after`, every matching subscription
        (up to 2,000)."""
        params: Dict[str, Any] = {"limit": _PAGE_SIZE, "sorting": "-started_at"}
        if started_after is not None:
            params["started_after"] = started_after.isoformat()
        if statuses:
            params["status"] = list(statuses)
        items: List[Dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            data = await self._request("GET", "/subscriptions/", params={**params, "page": page})
            items.extend(data.get("items") or [])
            if page >= int((data.get("pagination") or {}).get("max_page") or 1):
                break
        return items

    async def create_customer_session(self, customer_id: str, *, return_url: Optional[str] = None) -> Dict[str, Any]:
        """A one-off pre-authenticated link (`customer_portal_url`) to the
        customer portal, where they cancel or update their card."""
        body: Dict[str, Any] = {"customer_id": customer_id}
        if return_url:
            body["return_url"] = return_url
        return await self._request("POST", "/customer-sessions/", json=body)

    async def get_product(self, product_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/products/{product_id}")

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
        except httpx.TimeoutException as exc:
            raise TransientPlatformError(f"Polar request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientPlatformError(f"Polar request failed: {path}: {exc}") from exc

        if 200 <= response.status_code < 300:
            return response.json()

        detail = _detail(response)
        if response.status_code == 429:
            try:
                wait = float(response.headers.get("retry-after", _DEFAULT_RATE_LIMIT_WAIT_SECONDS))
            except ValueError:
                wait = _DEFAULT_RATE_LIMIT_WAIT_SECONDS
            raise RateLimitedError(f"Polar rate limited: {detail}", retry_after=wait)
        if response.status_code in (401, 403):
            raise PolarError(
                f"Polar rejected the access token ({response.status_code}): {detail} — check POLAR_ACCESS_TOKEN "
                "and that it has the checkouts:write, subscriptions:read and customer_sessions:write scopes"
            )
        if response.status_code in (400, 404, 409, 422):
            raise PermanentPlatformError(f"Polar {response.status_code}: {detail}")
        raise TransientPlatformError(f"Polar error {response.status_code}: {detail}")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(body, dict):
        return str(body)[:200]
    detail = body.get("detail") or body.get("error_description") or body.get("error") or ""
    if isinstance(detail, list):  # 422: [{"loc": [...], "msg": "..."}]
        return "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
            for item in detail
            if isinstance(item, dict)
        )[:300]
    return str(detail)[:300]
