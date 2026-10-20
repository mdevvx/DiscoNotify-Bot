"""Builders for TikTok creator-embed pages and their data."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Video IDs start with their upload time (Unix seconds << 32).
_BASE_SECONDS = 1_790_000_000


def video_id(n: int) -> str:
    return str(((_BASE_SECONDS + n * 3600) << 32) | 12345)


def video(n: int, *, private: bool = False, desc: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": video_id(n),
        "desc": f"caption {n}" if desc is None else desc,
        "coverUrl": f"https://cdn.example/cover{n}.jpg",
        "originCoverUrl": f"https://cdn.example/origin{n}.jpg",
        "playCount": 1000 + n,
        "privateItem": private,
        "authorUniqueId": "nasa",
    }


def user(*, uid: str = "107955", handle: str = "nasa", private: bool = False) -> Dict[str, Any]:
    return {
        "id": uid,
        "uniqueId": handle,
        "nickname": "NASA",
        "avatarThumbUrl": "https://cdn.example/avatar.jpg",
        "privateAccount": private,
        "code": 200,
    }


def page(
    videos: Any,
    *,
    account: Optional[Dict[str, Any]] = None,
    handle: str = "nasa",
    error: bool = False,
) -> str:
    state = {
        "source": {
            "data": {
                f"/embed/@{handle}": {
                    "isError": error,
                    "userInfo": account if account is not None else user(handle=handle),
                    "videoList": videos,
                },
                "strategy": {"page_context": {}},
            }
        }
    }
    return f'<html><script id="__FRONTITY_CONNECT_STATE__" type="application/json">{json.dumps(state)}</script></html>'


BOT_CHECK_PAGE = "<html><body>Please wait...</body></html>"
