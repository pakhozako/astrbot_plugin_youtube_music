"""Regression coverage for bounded persistent stores."""

import copy
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from filesystem_helpers import directory_link

from core.errors import UserFacingError
from core.library import UserLibrary
from core.persistence import (
    JSON_MAX_DEPTH,
    JsonStore,
    StorageError,
    StorageValidationError,
    strict_json_loads,
)
from core.state import QueueState
from core.utils import Song


class CapacityTests(unittest.TestCase):
    def test_capacity_rejects_new_owner_without_destroying_existing_data(self):
        song = Song(
            "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for section in ("favorites", "history"):
                with self.subTest(section=section):
                    payload = {"schema_version": 1, "favorites": {}, "history": {}}
                    payload[section] = {f"{index:064x}": [] for index in range(10000)}
                    path = root / f"{section}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    store = UserLibrary(path)
                    before = store.path.read_bytes()
                    with self.assertRaises(ValueError):
                        if section == "favorites":
                            store.add_favorite(("new", "user"), song)
                        else:
                            store.add_history_for_owner_id("f" * 64, song)
                    self.assertEqual(store.path.read_bytes(), before)
                    self.assertEqual(len(store.store.snapshot()[section]), 10000)
                    self.assertFalse(list(root.glob(f"{store.path.name}.corrupt-*")))

    def test_transactions_do_not_lose_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(
                Path(directory) / "counter.json",
                {"count": 0},
                lambda data: isinstance(data["count"], int),
            )
            with ThreadPoolExecutor(8) as executor:
                list(
                    executor.map(
                        lambda _: store.update(lambda data: data.update(count=data["count"] + 1)),
                        range(50),
                    )
                )
            self.assertEqual(store.snapshot()["count"], 50)
            self.assertEqual(json.loads(store.path.read_text())["count"], 50)

    def test_invalid_or_old_schema_is_backed_up_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for raw in ("{broken", '{"schema_version":0}', '{"schema_version":99}'):
                path = Path(directory) / "state.json"
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(StorageError):
                    QueueState(path)
                self.assertFalse(path.exists())
            self.assertEqual(len(list(Path(directory).glob("state.json.corrupt-*"))), 3)

    def test_corrupt_backups_preserve_each_payload_with_a_frozen_clock(self):
        payloads = ("{broken", '{"schema_version":0}', '{"schema_version":99}')
        with tempfile.TemporaryDirectory() as directory, patch("time.time_ns", return_value=1):
            path = Path(directory) / "state.json"
            for raw in payloads:
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(StorageError):
                    QueueState(path)
                self.assertFalse(path.exists())
            backups = list(Path(directory).glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), len(payloads))
            self.assertEqual(
                {backup.read_text(encoding="utf-8") for backup in backups}, set(payloads)
            )
            with self.assertRaisesRegex(StorageError, "resolve the saved corrupt backup"):
                QueueState(path)

    def test_failed_write_keeps_memory_and_disk_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(Path(directory) / "counter.json", {"count": 0}, lambda data: True)
            store.update(lambda data: data.update(count=1))
            with patch("core.persistence.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.update(lambda data: data.update(count=2))
            self.assertEqual(store.snapshot()["count"], 1)
            self.assertEqual(json.loads(store.path.read_text())["count"], 1)

    def test_ambiguous_nonfinite_and_deep_json_is_backed_up_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, raw in enumerate(
                (
                    '{"schema_version":1,"schema_version":1,"sessions":{},"queues":{},"paused":[]}',
                    '{"value":NaN}',
                    '{"value":Infinity}',
                    '{"value":1e309}',
                    '{"nested":' + "[" * 3000 + "0" + "]" * 3000 + "}",
                )
            ):
                path = root / f"state-{index}.json"
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(StorageError):
                    QueueState(path)
                self.assertFalse(path.exists())
                backups = list(root.glob(f"{path.name}.corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), raw)

    def test_strict_json_parser_accepts_standard_values_and_rejects_nested_ambiguity(self):
        self.assertEqual(
            strict_json_loads(b'{"list":[null,true,1,1.5],"text":"value"}'),
            {"list": [None, True, 1, 1.5], "text": "value"},
        )
        self.assertEqual(strict_json_loads("[1e308]"), [1e308])
        for raw in (
            '{"nested":{"key":1,"key":2}}',
            "[NaN]",
            "[-Infinity]",
            '{"nested":[1e309]}',
            "[-1e309]",
        ):
            with self.assertRaises(ValueError):
                strict_json_loads(raw)

    def test_json_container_depth_accepts_boundary_and_rejects_the_next_level(self):
        for opening, closing in (("[", "]"), ('{"nested":', "}")):
            with self.subTest(opening=opening):
                raw = opening * JSON_MAX_DEPTH + "0" + closing * JSON_MAX_DEPTH
                self.assertEqual(strict_json_loads(raw), json.loads(raw))
                with self.assertRaisesRegex(ValueError, "depth limit"):
                    strict_json_loads(opening + raw + closing)

    def test_json_container_depth_counts_mixed_objects_arrays_and_empty_containers(self):
        for terminal in ("0", "[]", "{}"):
            raw = terminal
            for depth in range(JSON_MAX_DEPTH - (terminal != "0")):
                raw = "[" + raw + "]" if depth % 2 == 0 else '{"nested":' + raw + "}"
            with self.subTest(terminal=terminal):
                self.assertEqual(strict_json_loads(raw), json.loads(raw))
                with self.assertRaisesRegex(ValueError, "depth limit"):
                    strict_json_loads("[" + raw + "]")

    def test_json_depth_does_not_depend_on_a_high_python_recursion_limit(self):
        original_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(max(original_limit, 10000))
            for depth in (JSON_MAX_DEPTH + 1, 3000):
                for opening, closing in (("[", "]"), ('{"nested":', "}")):
                    with self.subTest(depth=depth, opening=opening):
                        with self.assertRaisesRegex(ValueError, "depth limit"):
                            strict_json_loads(opening * depth + "0" + closing * depth)
        finally:
            sys.setrecursionlimit(original_limit)

    def test_json_parser_recursion_error_is_normalized_to_value_error(self):
        with patch("core.persistence.json.loads", side_effect=RecursionError):
            with self.assertRaisesRegex(ValueError, "depth limit") as caught:
                strict_json_loads("{}")
        self.assertIs(type(caught.exception), ValueError)

    def test_json_depth_guard_accepts_wide_data_and_literal_bracket_text(self):
        payload = {
            "rows": [{"nested": []} for _ in range(10000)],
            "text": "[" * 3000 + "}" * 3000,
        }
        self.assertEqual(strict_json_loads(json.dumps(payload)), payload)

    def test_parent_link_replacement_cannot_redirect_store_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "managed"
            store = JsonStore(parent / "state.json", {"count": 0}, lambda data: True)
            store.update(lambda data: data.update(count=1))
            parent.rename(root / "original")
            target = root / "target"
            target.mkdir()
            sentinel = target / store.path.name
            sentinel.write_text("unrelated data", encoding="utf-8")
            with directory_link(parent, target):
                with self.assertRaises(StorageError):
                    store.update(lambda data: data.update(count=2))
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated data")
                self.assertEqual(store.snapshot()["count"], 1)
                self.assertEqual(list(target.iterdir()), [sentinel])

    def test_invalid_transactions_remain_value_errors_and_are_user_facing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(
                Path(directory) / "counter.json", {"count": 0}, lambda data: data["count"] < 2
            )
            with self.assertRaises(StorageValidationError) as caught:
                store.update(lambda data: data.update(count=2))
            self.assertIsInstance(caught.exception, ValueError)
            self.assertIsInstance(caught.exception, UserFacingError)
            self.assertEqual(store.snapshot()["count"], 0)
            self.assertFalse(store.path.exists())

    def test_full_favorites_are_user_facing_and_keep_existing_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            library = UserLibrary(Path(directory) / "library.json", max_favorites=1)
            owner = ("session", "user")
            song = Song(
                "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
            )
            other = Song(
                "bcdefghijkl", "Other", "Artist", 60, "https://www.youtube.com/watch?v=bcdefghijkl"
            )
            library.add_favorite(owner, song)
            before = library.path.read_bytes()
            with self.assertRaisesRegex(UserFacingError, "收藏已满"):
                library.add_favorite(owner, other)
            self.assertEqual(library.path.read_bytes(), before)
            self.assertEqual(library.favorites(owner), [song])

    def test_duplicate_library_identities_are_preserved_as_corrupt_backups(self):
        song = Song(
            "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for section, timestamp in (("favorites", "added_at"), ("history", "played_at")):
                row = {"song": asdict(song), timestamp: 1}
                if section == "history":
                    row["play_count"] = 1
                payload = {"schema_version": 1, "favorites": {}, "history": {}}
                payload[section] = {"f" * 64: [row, row]}
                path = root / f"{section}.json"
                raw = json.dumps(payload)
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(StorageError):
                    UserLibrary(path)
                self.assertFalse(path.exists())
                backups = list(root.glob(f"{path.name}.corrupt-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), raw)

    def test_scoped_and_full_snapshots_and_missing_defaults_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(
                Path(directory) / "state.json", {"rows": {"owner": [{"value": 1}]}}, lambda _: True
            )
            full = store.snapshot()
            scoped = store.snapshot("rows", "owner")
            full["rows"]["owner"][0]["value"] = 2
            scoped[0]["value"] = 3
            default = [{"fallback": []}]
            missing = store.snapshot("rows", "missing", default=default)
            missing[0]["fallback"].append("changed")
            self.assertEqual(store.snapshot(), {"rows": {"owner": [{"value": 1}]}})
            self.assertEqual(store.snapshot("rows", "owner"), [{"value": 1}])
            self.assertEqual(default, [{"fallback": []}])
            self.assertEqual(store.snapshot("rows", "missing", default=default), [{"fallback": []}])
            self.assertIsNone(store.snapshot("missing"))

    def test_library_queries_copy_only_the_current_owner_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            library = UserLibrary(Path(directory) / "library.json")
            owner = ("session", "user")
            other_owner = ("session", "another")
            song = Song(
                "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
            )
            other = Song(
                "bcdefghijkl", "Other", "Artist", 60, "https://www.youtube.com/watch?v=bcdefghijkl"
            )
            for identity, track in ((owner, song), (other_owner, other)):
                library.add_favorite(identity, track)
                library.add_history_for_owner_id(library.owner_id(identity), track)
            with patch("core.persistence.copy.deepcopy", wraps=copy.deepcopy) as clone:
                self.assertEqual(library.favorites(owner), [song])
                history = library.history(owner)
            self.assertEqual(history[0]["song"]["video_id"], song.video_id)
            self.assertEqual(clone.call_count, 2)
            for call in clone.call_args_list:
                copied = call.args[0]
                self.assertIsInstance(copied, list)
                self.assertEqual([row["song"]["video_id"] for row in copied], [song.video_id])


if __name__ == "__main__":
    unittest.main()
