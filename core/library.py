"""Owner-scoped favorites and history; call transactions via run_blocking."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict
from pathlib import Path

from .errors import UserFacingError
from .persistence import JsonStore
from .queue import MAX_BATCH_SIZE
from .store_validation import valid_int, valid_number, valid_owner, valid_song
from .utils import Song


class UserLibrary:
    MAX_OWNERS = 10000
    MAX_PLAY_COUNT = 1000000000000

    def __init__(self, path: Path, *, max_history: int = 100, max_favorites: int = 100):
        self.path = Path(path)
        self.max_history = max(1, min(500, max_history))
        self.max_favorites = max(1, min(500, max_favorites))
        self.store = JsonStore(
            self.path, {"schema_version": 1, "favorites": {}, "history": {}}, self._valid_payload
        )

    @staticmethod
    def owner_id(owner_key: tuple[str, str]) -> str:
        return hashlib.sha256("\x1f".join(owner_key).encode("utf-8")).hexdigest()

    @classmethod
    def _valid_payload(cls, payload: dict) -> bool:
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or set(payload) != {"schema_version", "favorites", "history"}
        ):
            return False
        for section, timestamp in (("favorites", "added_at"), ("history", "played_at")):
            groups = payload.get(section)
            if not isinstance(groups, dict) or len(groups) > cls.MAX_OWNERS:
                return False
            total = 0
            for owner, rows in groups.items():
                if not valid_owner(owner) or not isinstance(rows, list) or len(rows) > 500:
                    return False
                total += len(rows)
                if total > 50000:
                    return False
                identities = set()
                for row in rows:
                    if not isinstance(row, dict) or not valid_song(row.get("song")):
                        return False
                    video_id = row["song"]["video_id"]
                    if video_id in identities:
                        return False
                    identities.add(video_id)
                    expected = (
                        {"song", timestamp, "play_count"}
                        if section == "history"
                        else {"song", timestamp}
                    )
                    if set(row) != expected:
                        return False
                    if not valid_number(row.get(timestamp)):
                        return False
                    if section == "history" and not valid_int(
                        row.get("play_count"), 1, cls.MAX_PLAY_COUNT
                    ):
                        return False
        return True

    def favorites(self, owner_key: tuple[str, str]) -> list[Song]:
        return [
            Song(**row["song"])
            for row in self.store.snapshot("favorites", self.owner_id(owner_key), default=[])
        ]

    def songs_for_source(
        self, owner_key: tuple[str, str], source: str, video_ids: list[str]
    ) -> list[Song]:
        if not isinstance(source, str) or source not in {"favorites", "history"}:
            raise UserFacingError("歌曲来源无效。")
        if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= MAX_BATCH_SIZE:
            raise UserFacingError(f"请一次选择 1 至 {MAX_BATCH_SIZE} 首歌曲。")
        if any(
            not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id)
            for video_id in video_ids
        ) or len(set(video_ids)) != len(video_ids):
            raise UserFacingError("歌曲选择无效，请重新选择。")
        rows = self.store.snapshot(source, self.owner_id(owner_key), default=[])
        songs = {row["song"]["video_id"]: row["song"] for row in rows}
        if any(video_id not in songs for video_id in video_ids):
            raise UserFacingError("歌曲已不在当前列表中，请刷新后重新选择。")
        return [Song(**songs[video_id]) for video_id in video_ids]

    def favorite_from_history(self, owner_key: tuple[str, str], video_id: str) -> tuple[bool, int]:
        song = self.songs_for_source(owner_key, "history", [video_id])[0]
        return self.add_favorite(owner_key, song)

    def add_favorite(self, owner_key: tuple[str, str], song: Song) -> tuple[bool, int]:
        owner = self.owner_id(owner_key)

        def mutate(payload):
            rows = payload["favorites"].get(owner, [])
            if any(row["song"]["video_id"] == song.video_id for row in rows):
                return False, len(rows)
            if len(rows) >= self.max_favorites:
                raise UserFacingError("收藏已满，请先移除一首歌曲。")
            rows.insert(0, {"song": asdict(song), "added_at": time.time()})
            payload["favorites"][owner] = rows
            return True, len(rows)

        return self.store.update(mutate)

    def remove_favorite(self, owner_key: tuple[str, str], video_id: str) -> bool:
        owner = self.owner_id(owner_key)

        def mutate(payload):
            rows = payload["favorites"].get(owner, [])
            kept = [row for row in rows if row["song"]["video_id"] != video_id]
            if kept:
                payload["favorites"][owner] = kept
            else:
                payload["favorites"].pop(owner, None)
            return len(kept) != len(rows)

        return self.store.update(mutate)

    def add_history_for_owner_id(self, owner: str, song: Song) -> None:
        def mutate(payload):
            rows = payload["history"].get(owner, [])
            previous = next((row for row in rows if row["song"]["video_id"] == song.video_id), None)
            count = min(self.MAX_PLAY_COUNT, previous["play_count"] + 1) if previous else 1
            rows = [row for row in rows if row["song"]["video_id"] != song.video_id]
            rows.insert(0, {"song": asdict(song), "played_at": time.time(), "play_count": count})
            payload["history"][owner] = rows[: self.max_history]

        self.store.update(mutate)

    def history(self, owner_key: tuple[str, str]) -> list[dict]:
        return self.store.snapshot("history", self.owner_id(owner_key), default=[])

    def clear_history(self, owner_key: tuple[str, str]) -> None:
        self.store.update(lambda payload: payload["history"].pop(self.owner_id(owner_key), None))
