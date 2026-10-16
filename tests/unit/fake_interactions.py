"""Stand-ins for discord.Interaction's response/followup, shared by the
command tests. Every reply is a Components V2 card (ui/cards.py), so each
recorded message keeps the layout under "view" and its flattened text
under "content" — tests assert on what a card says, not how it's nested.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ui.cards import plain_text


def _record(view: Any, **extra: Any) -> Dict[str, Any]:
    return {"content": plain_text(view), "view": view, **extra}


class FakeResponse:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.modals: List[Any] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages) or bool(self.edits) or bool(self.modals)

    async def send_message(
        self, content: Optional[str] = None, *, view: Any = None, ephemeral: bool = False, allowed_mentions: Any = None
    ) -> None:
        assert content is None, "replies must be Components V2 cards, not plain content"
        self.messages.append(_record(view, ephemeral=ephemeral, allowed_mentions=allowed_mentions))

    async def edit_message(self, *, view: Any = None) -> None:
        self.edits.append(_record(view))

    async def send_modal(self, modal: Any) -> None:
        self.modals.append(modal)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def send(
        self, content: Optional[str] = None, *, view: Any = None, ephemeral: bool = False, allowed_mentions: Any = None
    ) -> None:
        assert content is None, "replies must be Components V2 cards, not plain content"
        self.messages.append(_record(view, ephemeral=ephemeral, allowed_mentions=allowed_mentions))


class FakePermissions:
    def __init__(self, *, mention_everyone: bool = True, administrator: bool = False) -> None:
        self.mention_everyone = mention_everyone
        self.administrator = administrator


class FakeUser:
    """A server member. Has Mention Everyone by default, so tests that set
    @everyone/@here mentions pass the permission check unless they opt out."""

    def __init__(self, user_id: int, *, mention_everyone: bool = True) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.guild_permissions = FakePermissions(mention_everyone=mention_everyone)
