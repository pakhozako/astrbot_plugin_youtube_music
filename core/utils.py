"""Small pure helpers for the YouTube music plugin."""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store_validation import valid_thumbnail_url

COMMAND_NAMES = ("点歌",)


def safe_text(value: object, limit: int = 180) -> str:
    """Bound text reflected to chat and hide untrusted URL parameters."""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())
    text = re.sub(r"(?:https?|socks5)://\S+", "[链接]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(token|cookie|password|passwd|secret|authorization)=\S+", r"\1=[隐藏]", text
    )
    return text[:limit]


_JOB_OWNERSHIP_MARKER = ".astrbot-youtube-music-job"
_JOB_OWNERSHIP_VALUE = "astrbot_plugin_youtube_music\n"


def mark_owned_job_dir(path: Path) -> None:
    """Write a plugin ownership marker into a freshly-created temporary job directory."""
    path = Path(path)
    marker = path / _JOB_OWNERSHIP_MARKER
    marker.write_text(_JOB_OWNERSHIP_VALUE, encoding="utf-8")


def cleanup_stale_owned_job_dirs(job_root: Path, cutoff_timestamp: float) -> int:
    """Remove only stale direct children that carry this plugin's ownership marker.

    Never scans a caller-provided cache root and never follows directory or marker symlinks.
    Unknown directories without a marker are intentionally left untouched.
    """
    root = Path(job_root)
    if root.is_symlink() or not root.is_dir():
        return 0
    root_real = root.resolve(strict=True)
    removed = 0
    for item in root.iterdir():
        if item.is_symlink() or not item.is_dir() or not item.name.startswith("job-"):
            continue
        try:
            item_real = item.resolve(strict=True)
            if item_real.parent != root_real:
                continue
            marker = item / _JOB_OWNERSHIP_MARKER
            if marker.is_symlink() or not marker.is_file():
                continue
            if marker.read_text(encoding="utf-8") != _JOB_OWNERSHIP_VALUE:
                continue
            if item.stat(follow_symlinks=False).st_mtime < cutoff_timestamp:
                shutil.rmtree(item)
                removed += 1
        except (OSError, UnicodeError):
            # Leave a directory in place if its ownership or age cannot be established.
            continue
    return removed


@dataclass(frozen=True, slots=True)
class Song:
    video_id: str
    title: str
    artist: str
    duration: int | None
    url: str
    thumbnail_url: str = ""


def _safe_thumbnail_url(value: object) -> str:
    url = str(value or "").strip()
    return url if valid_thumbnail_url(url) else ""


def _duration_seconds(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return math.ceil(duration) if math.isfinite(duration) and duration > 0 else None


def extract_command_argument(message_text: str | None) -> str:
    """Handle both event.message_str variants: with or without the command itself."""
    text = (message_text or "").strip()
    if not text:
        return ""
    alternatives = "|".join(
        re.escape(name) for name in sorted(COMMAND_NAMES, key=len, reverse=True)
    )
    pattern = re.compile(rf"^/?(?:{alternatives})(?:@\S+)?(?:\s+|$)", re.IGNORECASE)
    match = pattern.match(text)
    if match:
        return text[match.end() :].strip()
    return text


def parse_youtube_url(value: str) -> str | None:
    """Return a normalized YouTube URL, or None if value is not an allowed URL."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 2048:
        return None

    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    if hostname not in {
        "youtu.be",
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        return None
    video_id = youtube_video_id(candidate)
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def youtube_video_id(value: str) -> str:
    """Extract a YouTube video ID from a supported watch/short/embed URL."""
    try:
        parsed = urlsplit((value or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
        return ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    else:
        query = parse_qs(parsed.query)
        if parsed.path.rstrip("/") == "/watch":
            candidate = (query.get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = (
                parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"} else ""
            )
    # YouTube IDs are normally 11 URL-safe characters. Validate instead of accepting arbitrary path data.
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", candidate or "") else ""


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "时长未知"
    try:
        total = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        return "时长未知"
    if total < 0:
        return "时长未知"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def song_from_flat_entry(entry: dict) -> Song | None:
    video_id = str(entry.get("id") or "").strip()
    title = str(entry.get("title") or "（无标题）").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        return None
    # Flat search results sometimes expose a relative URL or an extractor-specific URL;
    # a canonical watch URL prevents accidentally passing a stream URL back to yt-dlp.
    url = f"https://www.youtube.com/watch?v={video_id}"
    duration = _duration_seconds(entry.get("duration"))
    artist = str(
        entry.get("channel") or entry.get("uploader") or entry.get("creator") or "未知频道"
    ).strip()
    thumbnail = entry.get("thumbnail")
    if not thumbnail and isinstance(entry.get("thumbnails"), list):
        for item in reversed(entry["thumbnails"]):
            if isinstance(item, dict) and item.get("url"):
                thumbnail = item.get("url")
                break
    return Song(
        video_id=video_id,
        title=safe_text(title, 200),
        artist=safe_text(artist, 120),
        duration=duration,
        url=url,
        thumbnail_url=_safe_thumbnail_url(thumbnail),
    )


def song_from_full_info(info: dict) -> Song:
    video_id = str(info.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise ValueError("invalid YouTube video identity")
    url = f"https://www.youtube.com/watch?v={video_id}"
    duration = _duration_seconds(info.get("duration"))
    return Song(
        video_id=video_id,
        title=safe_text(info.get("title") or "（无标题）", 200),
        artist=safe_text(
            info.get("channel") or info.get("uploader") or info.get("creator") or "未知频道", 120
        ),
        duration=duration,
        url=url,
        thumbnail_url=_safe_thumbnail_url(info.get("thumbnail")),
    )
