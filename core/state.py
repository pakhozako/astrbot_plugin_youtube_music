"""Single persistent source for known QQ sessions and FIFO queues."""

import hashlib

from .persistence import JsonStore
from .store_validation import valid_number, valid_owner, valid_song, valid_text


def session_key(platform_id: str, group_id: str, umo: str) -> str:
    identity = f"{platform_id}:group:{group_id}" if group_id else umo
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def valid_state(payload: dict) -> bool:
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or set(payload) != {"schema_version", "sessions", "queues", "paused"}
    ):
        return False
    sessions = payload.get("sessions")
    queues = payload.get("queues")
    paused = payload.get("paused")
    if not isinstance(sessions, dict) or len(sessions) > 200:
        return False
    if not isinstance(queues, dict) or not isinstance(paused, list) or len(paused) > 200:
        return False
    if any(not isinstance(key, str) for key in paused) or len(set(paused)) != len(paused):
        return False
    if any(key not in sessions for key in (*queues, *paused)):
        return False
    for key, session in sessions.items():
        if (
            not isinstance(key, str)
            or len(key) != 24
            or any(c not in "0123456789abcdef" for c in key)
        ):
            return False
        if not isinstance(session, dict) or set(session) != {"umo", "label", "group_id"}:
            return False
        if not all(
            valid_text(session.get(field), maximum)
            for field, maximum in (("umo", 512), ("label", 120))
        ):
            return False
        if not isinstance(session.get("group_id"), str) or len(session["group_id"]) > 24:
            return False
    total = 0
    for rows in queues.values():
        if not isinstance(rows, list) or len(rows) > 50:
            return False
        total += len(rows)
        entry_ids = set()
        video_ids = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "song", "owner", "requested_by", "added_at"}
                or not valid_song(row.get("song"))
            ):
                return False
            entry_id = row.get("id")
            if (
                not valid_owner(row.get("owner"))
                or not isinstance(entry_id, str)
                or len(entry_id) != 32
                or any(c not in "0123456789abcdef" for c in entry_id)
            ):
                return False
            video_id = row["song"]["video_id"]
            if entry_id in entry_ids or video_id in video_ids:
                return False
            entry_ids.add(entry_id)
            video_ids.add(video_id)
            if not valid_text(row.get("requested_by"), 80) or not valid_number(row.get("added_at")):
                return False
    return total <= 1000


class QueueState(JsonStore):
    def __init__(self, path):
        super().__init__(
            path, {"schema_version": 1, "sessions": {}, "queues": {}, "paused": []}, valid_state
        )
