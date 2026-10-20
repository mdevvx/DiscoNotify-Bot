"""Owns the Supabase async client and its connection lifecycle.

Nothing outside this module should call `create_async_client` directly —
routing every query through one `Database` instance keeps connect/close a
single well-defined lifecycle event instead of something every repository
has to manage itself.
"""

from __future__ import annotations

from typing import Optional

from supabase import AsyncClient, AsyncClientOptions, PostgrestAPIError, create_async_client

from utils.errors import DatabaseError
from utils.logger import get_logger

logger = get_logger(__name__)

# Postgres SQLSTATE for a unique-constraint violation, forwarded verbatim by
# Postgrest as APIError.code. Repositories doing get-or-create or
# insert-if-new use this to tell "someone already inserted this row" (safe
# to recover from) apart from every other insert failure (not safe to
# swallow).
UNIQUE_VIOLATION = "23505"

# Every DiscoNotify table lives in this schema rather than `public` (see
# migrations/0005). It must also be listed under the project's exposed
# Data API schemas, or Postgrest rejects every query.
DB_SCHEMA = "disconotify"


def is_unique_violation(error: Exception) -> bool:
    return isinstance(error, PostgrestAPIError) and error.code == UNIQUE_VIOLATION


class Database:
    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key
        self._client: Optional[AsyncClient] = None

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise DatabaseError("Database accessed before connect() completed.")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        self._client = await create_async_client(
            self._url, self._key, options=AsyncClientOptions(schema=DB_SCHEMA)
        )
        # Fail fast at startup if credentials or the schema are wrong, rather
        # than surfacing a confusing error on the first command a user runs.
        await self._client.table("notify_guilds").select("guild_id").limit(1).execute()

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.postgrest.aclose()
        except Exception:
            logger.warning("Error while closing the Supabase client session.", exc_info=True)
        finally:
            self._client = None
