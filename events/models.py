"""The normalized event model every platform adapter produces (section 27).

Adapters never see or set `guild_id`/`account_id` — those are our internal
database identifiers, and a given piece of platform content is normalized
once per adapter call but may end up attached to a specific guild's
monitoring only after the fact. `PlatformAdapter.normalize_event()` returns
an event with both left as None; `monitoring.worker.AccountWorker` (which
*does* know which guild/account it's polling for) fills them in via
`dataclasses.replace()` before the event goes anywhere near the database.

The EventType values here must stay in sync with the `event_type_t`
Postgres domain in database/migrations/0002_core_schema.sql — this is the
Python-side single source of truth; that migration is the database-side one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


class EventType(str, enum.Enum):
    POST_CREATED = "post_created"
    VIDEO_PUBLISHED = "video_published"
    SHORT_PUBLISHED = "short_published"
    STREAM_STARTED = "stream_started"
    STREAM_ENDED = "stream_ended"
    STREAM_UPDATED = "stream_updated"
    ACCOUNT_UPDATED = "account_updated"


@dataclass(frozen=True)
class NormalizedEvent:
    platform: str
    platform_account_id: str
    account_username: str
    event_type: EventType
    # The platform's own identifier for this specific piece of content or
    # stream session — combined with (account, event_type) this is the
    # deduplication key checked against the `events` table.
    platform_event_id: str

    guild_id: Optional[int] = None
    account_id: Optional[str] = None  # platform_accounts.id (uuid), filled in by the worker

    title: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    author: Optional[str] = None  # the account's display name, when the platform provides one
    author_avatar_url: Optional[str] = None
    published_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
