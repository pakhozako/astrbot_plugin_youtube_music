"""AstrBot configuration is the only authority for runtime policy."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _integer(config, key, default, minimum, maximum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _boolean(config, key, default):
    value = config.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _list(config, key):
    value = config.get(key, "")
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError(f"{key} must be a bounded string")
    return tuple(dict.fromkeys(item for item in re.split(r"[,;\s]+", value.strip()) if item))


def _proxy(config, key, schemes):
    value = config.get(key, "")
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(f"{key}: invalid proxy")
    value = value.strip()
    if value:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 32 for c in value)
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError(f"{key}: invalid proxy")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    yt_dlp_path: Path
    ffmpeg_path: Path
    ffprobe_path: Path
    deno_path: Path
    search_results: int
    result_ttl_minutes: int
    max_duration_seconds: int
    max_download_size_mb: int
    max_wav_size_mb: int
    search_timeout_seconds: int
    metadata_timeout_seconds: int
    download_timeout_seconds: int
    conversion_timeout_seconds: int
    max_concurrent_jobs: int
    send_concurrency: int
    audio_cache_retention_hours: int
    audio_cache_max_mb: int
    max_queued_jobs: int
    max_session_queue: int
    queue_wait_timeout_seconds: int
    send_timeout_seconds: int
    request_cooldown_seconds: int
    search_result_auto_recall: bool
    search_result_recall_delay_seconds: int
    dependency_update_timeout_seconds: int
    max_history_items: int
    max_favorites_per_user: int
    queue_admin_user_ids: tuple[str, ...]
    allowed_group_ids: tuple[str, ...]
    send_strategy: str
    youtube_proxy: str
    dependency_proxy: str

    @classmethod
    def from_config(cls, config, *, data_dir: Path):
        dep = Path(data_dir) / "dependencies"
        suffix = ".exe" if os.name == "nt" else ""
        numbers = {
            "search_results": (5, 3, 10),
            "result_ttl_minutes": (15, 1, 120),
            "max_duration_seconds": (180, 15, 900),
            "max_download_size_mb": (50, 5, 500),
            "max_wav_size_mb": (8, 1, 100),
            "search_timeout_seconds": (45, 10, 180),
            "metadata_timeout_seconds": (45, 10, 180),
            "download_timeout_seconds": (240, 30, 1800),
            "conversion_timeout_seconds": (90, 10, 600),
            "max_concurrent_jobs": (2, 1, 5),
            "send_concurrency": (2, 1, 5),
            "audio_cache_retention_hours": (24, 1, 720),
            "audio_cache_max_mb": (512, 32, 8192),
            "max_queued_jobs": (5, 0, 50),
            "max_session_queue": (20, 1, 50),
            "queue_wait_timeout_seconds": (120, 5, 900),
            "send_timeout_seconds": (60, 5, 300),
            "request_cooldown_seconds": (3, 0, 60),
            "search_result_recall_delay_seconds": (60, 5, 120),
            "dependency_update_timeout_seconds": (1800, 60, 7200),
            "max_history_items": (100, 10, 500),
            "max_favorites_per_user": (100, 10, 500),
        }
        strategy = config.get("send_strategy", "voice_then_link")
        if strategy not in {"voice_only", "voice_then_link", "link_only"}:
            raise ValueError("invalid send_strategy")
        return cls(
            yt_dlp_path=dep / "yt-dlp" / f"yt-dlp{suffix}",
            ffmpeg_path=dep / "FFmpeg" / "bin" / f"ffmpeg{suffix}",
            ffprobe_path=dep / "FFmpeg" / "bin" / f"ffprobe{suffix}",
            deno_path=dep / "Deno" / "bin" / f"deno{suffix}",
            **{key: _integer(config, key, *limits) for key, limits in numbers.items()},
            search_result_auto_recall=_boolean(config, "search_result_auto_recall", True),
            queue_admin_user_ids=_list(config, "queue_admin_user_ids"),
            allowed_group_ids=_list(config, "allowed_group_ids"),
            send_strategy=strategy,
            youtube_proxy=_proxy(config, "youtube_proxy", {"http", "https", "socks5"}),
            dependency_proxy=_proxy(config, "dependency_proxy", {"http", "https"}),
        )
