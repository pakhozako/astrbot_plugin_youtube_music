"""Shared validation primitives for untrusted, versioned plugin-local JSON stores."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

MAX_TIMESTAMP = 4102444800


def valid_number(value: Any, minimum: float = 0, maximum: float = MAX_TIMESTAMP) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and minimum <= value <= maximum
        and math.isfinite(value)
    )


def valid_int(value: Any, minimum: int = 0, maximum: int = 1000000000000) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def valid_owner(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def valid_text(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= limit
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def valid_thumbnail_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if not value:
        return True
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"i.ytimg.com", "img.youtube.com", "yt3.ggpht.com"}
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def valid_song(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "video_id",
        "title",
        "artist",
        "duration",
        "url",
        "thumbnail_url",
    }:
        return False
    video_id = value.get("video_id")
    duration = value.get("duration")
    return (
        isinstance(video_id, str)
        and bool(re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id))
        and valid_int(duration, 1, 86400)
        and valid_text(value.get("title"), 200)
        and valid_text(value.get("artist"), 120)
        and value.get("url") == f"https://www.youtube.com/watch?v={video_id}"
        and valid_thumbnail_url(value["thumbnail_url"])
    )
