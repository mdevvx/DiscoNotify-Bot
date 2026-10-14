"""In-memory stand-ins for the Supabase query builder, used by repository
tests so they exercise real select/insert/update-or-conflict logic without
a live database. Per the project's testing rules, unit tests must not
depend on a live external service.

Supports exactly the chain shapes the repositories in this project use:
`.select("*").eq(col, val)....execute()`, `.insert(payload).execute()`,
`.update(payload).eq(col, val)....execute()`. Unique constraints are
declared per table (`unique_on`) and a colliding insert raises a real
`PostgrestAPIError` with code 23505 — the same type and code
`database.client.is_unique_violation()` checks for against the real thing.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from supabase import PostgrestAPIError

_DefaultValue = Union[Any, Callable[[], Any]]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeQuery:
    def __init__(self, table: "FakeTable", op: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._table = table
        self._op = op
        self._payload = payload or {}
        self._filters: Dict[str, Any] = {}
        self._in_filters: Dict[str, List[Any]] = {}
        self._order: Optional[tuple] = None
        self._limit: Optional[int] = None

    def eq(self, column: str, value: Any) -> "_FakeQuery":
        self._filters[column] = value
        return self

    def in_(self, column: str, values: Sequence[Any]) -> "_FakeQuery":
        self._in_filters[column] = list(values)
        return self

    def order(self, column: str, *, desc: bool = False) -> "_FakeQuery":
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> "_FakeQuery":
        self._limit = count
        return self

    def _matching_rows(self) -> List[Dict[str, Any]]:
        rows = [row for row in self._table.rows if all(row.get(k) == v for k, v in self._filters.items())]
        for column, values in self._in_filters.items():
            rows = [row for row in rows if row.get(column) in values]
        return rows

    async def execute(self) -> _FakeResult:
        if self._op == "select":
            rows = [dict(row) for row in self._matching_rows()]
            if self._order is not None:
                column, desc = self._order
                rows.sort(key=lambda row: (row.get(column) is not None, row.get(column) or ""), reverse=desc)
            if self._limit is not None:
                rows = rows[: self._limit]
            return _FakeResult(rows)

        if self._op == "insert":
            resolved_defaults = {k: (v() if callable(v) else v) for k, v in self._table.defaults.items()}
            candidate = {**resolved_defaults, **self._payload}
            if self._table.has_generated_id and "id" not in candidate:
                candidate["id"] = str(uuid.uuid4())

            conflict = self._table.find_conflict(candidate)
            if conflict is not None:
                raise PostgrestAPIError(
                    {"code": "23505", "message": "duplicate key value violates unique constraint"}
                )

            self._table.rows.append(candidate)
            return _FakeResult([dict(candidate)])

        if self._op == "update":
            matched = self._matching_rows()
            for row in matched:
                row.update(self._payload)
            return _FakeResult([dict(row) for row in matched])

        if self._op == "delete":
            matched = self._matching_rows()
            for row in matched:
                self._table.rows.remove(row)
            return _FakeResult([dict(row) for row in matched])

        raise AssertionError(f"Unexpected op {self._op!r}")


class FakeTable:
    def __init__(
        self,
        *,
        unique_on: Sequence[Sequence[str]],
        defaults: Optional[Dict[str, _DefaultValue]] = None,
        has_generated_id: bool = False,
    ) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.defaults = defaults or {}
        self.has_generated_id = has_generated_id
        self._unique_on = [tuple(cols) for cols in unique_on]

    def select(self, *_columns: str) -> _FakeQuery:
        return _FakeQuery(self, "select")

    def insert(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "insert", payload=payload)

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload=payload)

    def delete(self) -> _FakeQuery:
        return _FakeQuery(self, "delete")

    def find_conflict(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for cols in self._unique_on:
            key = tuple(candidate.get(c) for c in cols)
            for row in self.rows:
                if tuple(row.get(c) for c in cols) == key:
                    return row
        return None


class _FakeSupabaseClient:
    def __init__(self, tables: Dict[str, FakeTable]) -> None:
        self._tables = tables

    def table(self, name: str) -> FakeTable:
        return self._tables[name]


class FakeDatabase:
    """Stands in for database.client.Database in repository tests."""

    def __init__(self) -> None:
        self.tables: Dict[str, FakeTable] = {
            "pulsenotify_guilds": FakeTable(unique_on=[("guild_id",)], defaults={"enabled": True}),
            "pulsenotify_platform_accounts": FakeTable(
                unique_on=[("id",), ("guild_id", "platform", "platform_account_id")],
                defaults={"enabled": True, "status": "active", "consecutive_failures": 0, "content_cursor": None},
                has_generated_id=True,
            ),
            "pulsenotify_events": FakeTable(
                unique_on=[("account_id", "event_type", "platform_event_id")],
                defaults={"notified": False, "metadata": {}},
                has_generated_id=True,
            ),
            "pulsenotify_live_states": FakeTable(unique_on=[("account_id",)], defaults={"is_live": False}),
            "pulsenotify_notification_channels": FakeTable(
                unique_on=[("id",), ("guild_id", "channel_id")],
                defaults={"is_valid": True, "name": None, "webhook_url": None},
                has_generated_id=True,
            ),
            "pulsenotify_account_channels": FakeTable(unique_on=[("account_id", "channel_id")]),
            "pulsenotify_alert_configurations": FakeTable(
                unique_on=[("id",), ("account_id", "event_type")],
                defaults={"enabled": True, "mention_role_id": None, "embed_enabled": True, "custom_message": None},
                has_generated_id=True,
            ),
            "pulsenotify_billing": FakeTable(
                unique_on=[("guild_id",)], defaults={"whitelisted": False, "paid_until": None}
            ),
            "pulsenotify_bot_state": FakeTable(unique_on=[("key",)]),
            "pulsenotify_payments": FakeTable(
                unique_on=[("provider", "external_id")],
                defaults={"status": "pending", "metadata": dict, "created_at": _now_iso},
                has_generated_id=True,
            ),
            "pulsenotify_platform_health": FakeTable(
                unique_on=[("platform",)],
                defaults={
                    "status": "disabled",
                    "consecutive_failures": 0,
                    "requests_today": 0,
                    "requests_today_reset_at": lambda: datetime.date.today().isoformat(),
                },
            ),
        }

    @property
    def client(self) -> _FakeSupabaseClient:
        return _FakeSupabaseClient(self.tables)
