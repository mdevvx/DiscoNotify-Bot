"""X embedded-timeline data in the shape the real page returns (captured
from syndication.twitter.com on 2026-10-14, trimmed)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

NASA = {
    "id_str": "11348282",
    "screen_name": "NASA",
    "name": "NASA",
    "profile_image_url_https": "https://pbs.twimg.com/profile_images/1321163587679784960/0ZxKlEKB_normal.jpg",
}


def post(post_id: str, **extra: Any) -> Dict[str, Any]:
    return {
        "id_str": post_id,
        "created_at": "Wed Oct 14 15:16:26 +0000 2026",
        "full_text": f"post {post_id}",
        "user": NASA,
        "entities": {},
        **extra,
    }


def photo_post() -> Dict[str, Any]:
    return post(
        "2110389272024867195",
        full_text="Watch it on our YouTube channel: https://t.co/qHxwCBOoMr https://t.co/wiLlrHDnRH",
        entities={"urls": [{"url": "https://t.co/qHxwCBOoMr", "expanded_url": "https://youtu.be/5b3n-u_0QAI"}]},
        extended_entities={
            "media": [
                {
                    "type": "photo",
                    "url": "https://t.co/wiLlrHDnRH",
                    "media_url_https": "https://pbs.twimg.com/media/HUmb2vRWwAAFQ3D.jpg",
                }
            ]
        },
    )


def retweet(post_id: str) -> Dict[str, Any]:
    return post(post_id, retweeted_status={"id_str": "1", "user": {"screen_name": "NASAArtemis"}})


def page(posts: List[Dict[str, Any]]) -> str:
    data = {
        "props": {
            "pageProps": {
                "timeline": {"entries": [{"type": "tweet", "content": {"tweet": item}} for item in posts]},
                "headerProps": {"screenName": "NASA"},
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'
