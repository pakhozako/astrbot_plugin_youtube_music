"""Persistent, bounded WAV cache keyed by YouTube video id and output format."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .persistence import strict_json_loads
from .store_validation import valid_int, valid_number, valid_song
from .utils import Song

FORMAT_KEY = "wav-pcm_s16le-16000-mono-v1"
CACHE_PREFIX = "astrbot-youtube-music-"
CACHE_OWNER = "astrbot_plugin_youtube_music"


def _cache_locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._thread_lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(slots=True)
class AudioCacheEntry:
    path: Path
    song: Song
    media: dict[str, Any]


class AudioCache:
    def __init__(self, directory: Path, *, retention_hours: int = 24, max_size_mb: int = 512):
        self.directory = Path(directory)
        self.retention_seconds = max(1, int(retention_hours)) * 3600
        self.max_size_bytes = max(1, int(max_size_mb)) * 1024 * 1024
        self._thread_lock = threading.RLock()
        self.protected_video_ids: set[str] = set()
        if self.directory.is_symlink() or self.directory.is_junction():
            raise ValueError("invalid cache directory")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._directory_real = self.directory.resolve(strict=True)

    def _directory_available(self) -> bool:
        try:
            return (
                not self.directory.is_symlink()
                and not self.directory.is_junction()
                and self.directory.is_dir()
                and self.directory.resolve(strict=True) == self._directory_real
            )
        except (OSError, RuntimeError):
            return False

    def _contains(self, path: Path) -> bool:
        try:
            return (
                self._directory_available()
                and path.parent.resolve(strict=True) == self._directory_real
                and not path.is_symlink()
                and not path.is_junction()
            )
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _key(video_id: str) -> str:
        return hashlib.sha256(f"{video_id}|{FORMAT_KEY}".encode()).hexdigest()

    def _paths(self, video_id: str) -> tuple[Path, Path]:
        key = self._key(video_id)
        return (
            self.directory / f"{CACHE_PREFIX}{key}.wav",
            self.directory / f"{CACHE_PREFIX}{key}.json",
        )

    def _owns_pair(self, wav_path: Path, meta_path: Path) -> bool:
        return (
            self._contains(wav_path)
            and self._contains(meta_path)
            and wav_path == meta_path.with_suffix(".wav")
            and bool(re.fullmatch(rf"{CACHE_PREFIX}[a-f0-9]{{64}}", meta_path.stem))
        )

    @staticmethod
    def _song_payload(song: Song) -> dict[str, Any]:
        return {
            "video_id": song.video_id,
            "title": song.title,
            "artist": song.artist,
            "duration": song.duration,
            "url": song.url,
            "thumbnail_url": song.thumbnail_url,
        }

    @staticmethod
    def _song_from_payload(payload: dict[str, Any]) -> Song:
        if not valid_song(payload):
            raise ValueError("invalid cached song")
        return Song(**payload)

    @classmethod
    def _read_metadata(cls, path: Path) -> dict[str, Any]:
        if path.stat().st_size > 32 * 1024:
            raise ValueError("cache metadata exceeds size limit")
        metadata = strict_json_loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            != {
                "owner",
                "format_key",
                "video_id",
                "song",
                "media",
                "created_at",
                "last_used_at",
                "wav_size_bytes",
            }
            or metadata["owner"] != CACHE_OWNER
            or metadata["format_key"] != FORMAT_KEY
            or not valid_song(metadata["song"])
            or metadata["video_id"] != metadata["song"]["video_id"]
            or path.stem != f"{CACHE_PREFIX}{cls._key(metadata['video_id'])}"
            or not isinstance(metadata["media"], dict)
            or not valid_number(metadata["created_at"], 1)
            or not valid_number(metadata["last_used_at"], 1)
            or not valid_int(metadata["wav_size_bytes"], 45)
        ):
            raise ValueError("invalid current cache metadata")
        return metadata

    @_cache_locked
    def lookup(self, video_id: str) -> AudioCacheEntry | None:
        if not video_id or not self._directory_available():
            return None
        wav_path, meta_path = self._paths(video_id)
        if (
            not wav_path.is_file()
            or not meta_path.is_file()
            or wav_path.is_symlink()
            or meta_path.is_symlink()
        ):
            # Orphaned cache files are safe to remove because their names are generated by this cache.
            if wav_path.exists() != meta_path.exists():
                self._remove_pair(wav_path, meta_path)
            return None
        try:
            metadata = self._read_metadata(meta_path)
            now = time.time()
            last_used = metadata["last_used_at"]
            size = wav_path.stat().st_size
            if (
                metadata["video_id"] != video_id
                or metadata["wav_size_bytes"] != size
                or now - last_used > self.retention_seconds
                or size <= 44
                or size > self.max_size_bytes
            ):
                self._remove_pair(wav_path, meta_path)
                return None
            with wav_path.open("rb") as stream:
                header = stream.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                self._remove_pair(wav_path, meta_path)
                return None
            song = self._song_from_payload(metadata["song"])
            media = {**metadata["media"], "cache_hit": True}
            metadata["last_used_at"] = now
            self._atomic_json(meta_path, metadata)
            try:
                os.utime(wav_path, (now, now))
            except OSError:
                pass
            return AudioCacheEntry(wav_path, song, media)
        except (OSError, ValueError, TypeError, OverflowError, AttributeError, RecursionError):
            self._remove_pair(wav_path, meta_path)
            return None

    @_cache_locked
    def copy_for_job(
        self, video_id: str, job_dir: Path, *, max_size_bytes: int | None = None
    ) -> AudioCacheEntry | None:
        """Copy a reusable cache entry to private job audio before releasing the cache lock."""
        directory = Path(job_dir)
        if not directory.is_dir() or any(
            path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)
        ):
            raise ValueError("invalid audio job directory")
        directory_real = directory.resolve(strict=True)
        if directory_real.is_relative_to(self._directory_real):
            raise ValueError("audio jobs cannot overwrite the shared cache")
        if max_size_bytes is not None and (
            not isinstance(max_size_bytes, int)
            or isinstance(max_size_bytes, bool)
            or max_size_bytes <= 44
        ):
            raise ValueError("invalid audio snapshot size limit")
        limit = min(self.max_size_bytes, max_size_bytes or self.max_size_bytes)
        destination = directory / "record.wav"

        def current_directory():
            try:
                return (
                    directory.is_dir()
                    and directory.resolve(strict=True) == directory_real
                    and not any(
                        path.is_symlink() or path.is_junction()
                        for path in (directory, *directory.parents)
                    )
                )
            except (OSError, RuntimeError):
                return False

        def regular_destination():
            return (
                not destination.is_symlink()
                and not destination.is_junction()
                and (not destination.exists() or destination.is_file())
            )

        if not regular_destination():
            raise ValueError("invalid audio job destination")
        entry = self.lookup(video_id)
        if entry is None:
            return None
        size = entry.path.stat().st_size
        if size > limit:
            return None
        temporary = directory / f".record.wav.{uuid.uuid4().hex}.tmp"
        try:
            if not current_directory():
                raise ValueError("audio job directory changed")
            copied = 0
            with entry.path.open("rb") as source, temporary.open("xb") as target:
                while chunk := source.read(64 * 1024):
                    copied += len(chunk)
                    if copied > limit:
                        raise ValueError("cached audio exceeds snapshot size limit")
                    target.write(chunk)
            if copied != size or copied <= 44:
                raise ValueError("cached audio changed while copying")
            if not current_directory() or not regular_destination():
                raise ValueError("audio job destination changed")
            os.replace(temporary, destination)
            return AudioCacheEntry(destination, entry.song, dict(entry.media))
        finally:
            if current_directory() and not temporary.is_symlink() and temporary.is_file():
                temporary.unlink(missing_ok=True)

    @_cache_locked
    def store(self, song: Song, wav_source: Path, media: dict[str, Any]) -> Path | None:
        if (
            not self._directory_available()
            or not valid_song(self._song_payload(song))
            or not isinstance(media, dict)
            or not wav_source.is_file()
            or wav_source.is_symlink()
        ):
            return None
        size = wav_source.stat().st_size
        if size <= 44 or size > self.max_size_bytes:
            return None
        wav_path, meta_path = self._paths(song.video_id)
        token = uuid.uuid4().hex
        temp_wav = self.directory / f".{wav_path.stem}.{token}.tmp"
        temp_meta = self.directory / f".{meta_path.stem}.{uuid.uuid4().hex}.tmp"
        now = time.time()
        metadata = {
            "owner": CACHE_OWNER,
            "format_key": FORMAT_KEY,
            "video_id": song.video_id,
            "song": self._song_payload(song),
            "media": {key: value for key, value in media.items() if key != "cache_hit"},
            "created_at": now,
            "last_used_at": now,
            "wav_size_bytes": size,
        }
        try:
            # Serialize before replacing either member of an existing cache pair.
            self._write_json(temp_meta, metadata)
            shutil.copyfile(wav_source, temp_wav)
            with temp_wav.open("rb") as stream:
                header = stream.read(12)
            if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            os.replace(temp_wav, wav_path)
            os.replace(temp_meta, meta_path)
            self.cleanup()
            return wav_path if wav_path.is_file() and meta_path.is_file() else None
        except (OSError, ValueError, TypeError, OverflowError):
            return None
        finally:
            for temp_path in (temp_wav, temp_meta):
                try:
                    if self._contains(temp_path):
                        temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @_cache_locked
    def invalidate(self, video_id: str) -> None:
        if not video_id:
            return
        self.delete(video_id)

    @_cache_locked
    def cleanup(self, protected_video_ids: set[str] | None = None) -> dict[str, int]:
        if not self._directory_available():
            return {"removed_expired": 0, "removed_capacity": 0, "total_bytes": 0, "items": 0}
        now = time.time()
        protected = set(
            self.protected_video_ids if protected_video_ids is None else protected_video_ids
        )
        removed_expired = 0
        records: list[tuple[float, Path, Path, int, str]] = []
        for meta_path in self.directory.glob("*.json"):
            wav_path = meta_path.with_suffix(".wav")
            if meta_path.is_symlink() or wav_path.is_symlink():
                continue
            try:
                if not self._owns_pair(wav_path, meta_path):
                    continue
                metadata = self._read_metadata(meta_path)
                last_used = metadata["last_used_at"]
                size = wav_path.stat().st_size if wav_path.is_file() else 0
                video_id = metadata["video_id"]
                invalid = not wav_path.is_file() or size <= 44 or metadata["wav_size_bytes"] != size
                expired = now - last_used > self.retention_seconds
                if invalid or (expired and video_id not in protected):
                    self._remove_pair(wav_path, meta_path)
                    removed_expired += 1
                    continue
                records.append((last_used, wav_path, meta_path, size, video_id))
            except (OSError, ValueError, TypeError, OverflowError, AttributeError, RecursionError):
                if self._owns_pair(wav_path, meta_path):
                    self._remove_pair(wav_path, meta_path)
                    removed_expired += 1
        # Only the plugin's explicit filename namespace proves ownership of orphans.
        known_json_stems = {p.stem for p in self.directory.glob("*.json")}
        for wav_path in self.directory.glob("*.wav"):
            if (
                re.fullmatch(rf"{CACHE_PREFIX}[a-f0-9]{{64}}", wav_path.stem)
                and not wav_path.is_symlink()
                and wav_path.stem not in known_json_stems
            ):
                if self._remove_pair(wav_path, wav_path.with_suffix(".json")):
                    removed_expired += 1
        for temp_path in self.directory.glob(".*.tmp"):
            if temp_path.is_symlink() or not re.fullmatch(
                rf"\.{CACHE_PREFIX}[a-f0-9]{{64}}\.[a-f0-9]{{32}}\.tmp", temp_path.name
            ):
                continue
            try:
                if self._contains(temp_path) and now - temp_path.stat().st_mtime > 3600:
                    temp_path.unlink()
            except OSError:
                pass
        total = sum(item[3] for item in records)
        removed_capacity = 0
        for _, wav_path, meta_path, size, video_id in sorted(records, key=lambda item: item[0]):
            if total <= self.max_size_bytes:
                break
            # Never evict a WAV while a send task may still be reading it.
            if video_id not in protected and self._remove_pair(wav_path, meta_path):
                total -= size
                removed_capacity += 1
        return {
            "removed_expired": removed_expired,
            "removed_capacity": removed_capacity,
            "total_bytes": max(0, total),
            "items": max(0, len(records) - removed_capacity),
        }

    @_cache_locked
    def list_entries(self) -> list[dict[str, Any]]:
        """List cache metadata without touching LRU timestamps."""
        rows: list[dict[str, Any]] = []
        if not self._directory_available():
            return rows
        for meta_path in self.directory.glob("*.json"):
            wav_path = meta_path.with_suffix(".wav")
            if meta_path.is_symlink() or wav_path.is_symlink():
                continue
            try:
                if not self._owns_pair(wav_path, meta_path):
                    continue
                metadata = self._read_metadata(meta_path)
                song = metadata["song"]
                video_id = metadata["video_id"]
                size = wav_path.stat().st_size if wav_path.is_file() else 0
                created = metadata["created_at"]
                last_used = metadata["last_used_at"]
                valid = bool(
                    wav_path.is_file()
                    and size > 44
                    and size <= self.max_size_bytes
                    and metadata["wav_size_bytes"] == size
                    and time.time() - last_used <= self.retention_seconds
                )
                if wav_path.is_file() and valid:
                    with wav_path.open("rb") as stream:
                        header = stream.read(12)
                    valid = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
                rows.append(
                    {
                        "video_id": video_id,
                        "title": song["title"],
                        "artist": song["artist"],
                        "duration_seconds": song["duration"],
                        "size_bytes": size,
                        "created_at": created,
                        "last_used_at": last_used,
                        "format_key": metadata["format_key"],
                        "valid": valid,
                    }
                )
            except (OSError, ValueError, TypeError, RecursionError):
                continue
        rows.sort(key=lambda row: row["last_used_at"], reverse=True)
        return rows

    @_cache_locked
    def delete(self, video_id: str) -> bool:
        if not video_id or not self._directory_available():
            return False
        wav_path, meta_path = self._paths(video_id)
        existed = wav_path.exists() or meta_path.exists()
        self._remove_pair(wav_path, meta_path)
        return existed and not wav_path.exists() and not meta_path.exists()

    @_cache_locked
    def clear_unprotected(self, protected_video_ids: set[str] | None = None) -> dict[str, int]:
        protected = set(
            self.protected_video_ids if protected_video_ids is None else protected_video_ids
        )
        deleted = 0
        skipped = 0
        for entry in self.list_entries():
            video_id = str(entry.get("video_id") or "")
            if not video_id:
                continue
            if video_id in protected:
                skipped += 1
                continue
            if self.delete(video_id):
                deleted += 1
        return {"deleted": deleted, "skipped_busy": skipped}

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            if stream.tell() > 32 * 1024:
                raise ValueError("cache metadata exceeds size limit")

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        if not self._contains(path):
            raise OSError("cache directory is unavailable")
        temp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
        try:
            self._write_json(temp, payload)
            os.replace(temp, path)
        finally:
            try:
                if self._contains(temp):
                    temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_pair(self, wav_path: Path, meta_path: Path) -> bool:
        if not self._owns_pair(wav_path, meta_path):
            return False
        removed = False
        for path in (wav_path, meta_path):
            try:
                if self._contains(path) and path.is_file():
                    path.unlink()
                    removed = True
            except OSError:
                pass
        return removed and not wav_path.exists() and not meta_path.exists()
