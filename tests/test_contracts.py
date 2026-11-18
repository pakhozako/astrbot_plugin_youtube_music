"""Configuration, URL, persistence shape and admission contracts."""

import asyncio
import json
import re
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path

from core.queue import JobLimiter, QueueFull, QueueWaitTimeout
from core.settings import Settings
from core.state import valid_state
from core.store_validation import valid_song, valid_thumbnail_url
from core.utils import (
    parse_youtube_url,
    safe_text,
    song_from_flat_entry,
    song_from_full_info,
)

ROOT = Path(__file__).resolve().parents[1]


class ContractsTests(unittest.TestCase):
    def test_configuration_reference_covers_every_default_and_integer_limit(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        document = (ROOT / "docs/CONFIGURATION.md").read_text(encoding="utf-8")
        rows = re.findall(r"^\| `(\w+)` \| `([^`]+)` \| ([^|]+) \|", document, re.MULTILINE)
        self.assertEqual({key for key, _, _ in rows}, set(schema))
        self.assertEqual(len(rows), len(schema))
        for key, default, bounds in rows:
            self.assertEqual(json.loads(default), schema[key]["default"], key)
            if schema[key]["type"] == "int":
                match = re.match(r"(\d+)\u2013(\d+)", bounds)
                self.assertIsNotNone(match, key)
                self.assertEqual(
                    tuple(map(int, match.groups())),
                    (schema[key]["slider"]["min"], schema[key]["slider"]["max"]),
                    key,
                )

    def test_schema_defaults_and_limits_match_the_runtime_configuration(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        settings = Settings.from_config(
            {key: value["default"] for key, value in schema.items()},
            data_dir=Path(tempfile.gettempdir()),
        )
        names = {field.name for field in fields(Settings)} - {
            "yt_dlp_path",
            "ffmpeg_path",
            "ffprobe_path",
            "deno_path",
        }
        self.assertEqual(set(schema), names)
        for key, spec in schema.items():
            if spec["type"] == "bool":
                for value in (0, 1, "true", None):
                    with self.assertRaises(ValueError):
                        Settings.from_config({key: value}, data_dir=Path(tempfile.gettempdir()))
            if spec["type"] == "int":
                for bound in ("min", "max"):
                    configured = Settings.from_config(
                        {key: spec["slider"][bound]}, data_dir=Path(tempfile.gettempdir())
                    )
                    self.assertEqual(getattr(configured, key), spec["slider"][bound])
                with self.assertRaises(ValueError):
                    Settings.from_config(
                        {key: spec["slider"]["max"] + 1}, data_dir=Path(tempfile.gettempdir())
                    )
        self.assertEqual(settings.send_strategy, "voice_then_link")

    def test_only_canonical_single_video_urls_reach_the_media_engine(self):
        self.assertEqual(
            parse_youtube_url("https://youtu.be/abcdefghijk?t=30&token=secret"),
            "https://www.youtube.com/watch?v=abcdefghijk",
        )
        for url in (
            "https://youtube.com.evil.test/watch?v=abcdefghijk",
            "https://api.youtube.com/watch?v=abcdefghijk",
            "file:///tmp/song",
            "https://youtube.com/playlist?list=abcdefghi",
        ):
            self.assertIsNone(parse_youtube_url(url))

    def test_stores_reject_noncanonical_urls_and_boolean_versions(self):
        from core.utils import Song

        song = asdict(
            Song(
                "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
            )
        )
        self.assertTrue(valid_song(song))
        song["url"] = "https://evil.test"
        self.assertFalse(valid_song(song))
        self.assertFalse(
            valid_state({"schema_version": True, "sessions": {}, "queues": {}, "paused": []})
        )
        song["url"] = "https://www.youtube.com/watch?v=abcdefghijk"
        song["unexpected"] = "field"
        self.assertFalse(valid_song(song))

    def test_error_text_does_not_leak_proxy_credentials(self):
        result = safe_text("socks5://user:password@proxy.test token=private")
        self.assertNotIn("password@", result)
        self.assertNotIn("private", result)

    def test_media_durations_reject_boolean_nonpositive_and_nonfinite_values(self):
        for duration in (True, False, 0, -1, "NaN", "Infinity", float("inf"), None):
            for convert in (song_from_flat_entry, song_from_full_info):
                with self.subTest(duration=duration, convert=convert.__name__):
                    song = convert({"id": "abcdefghijk", "duration": duration})
                    self.assertIsNone(song.duration)

    def test_fractional_media_duration_is_rounded_up_before_applying_limits(self):
        for convert in (song_from_flat_entry, song_from_full_info):
            self.assertEqual(convert({"id": "abcdefghijk", "duration": 180.9}).duration, 181)
            self.assertEqual(convert({"id": "abcdefghijk", "duration": "60"}).duration, 60)

    def test_thumbnail_normalization_and_store_validation_share_the_same_policy(self):
        valid = "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg?sqp=example"
        self.assertTrue(valid_thumbnail_url(valid))
        for convert in (song_from_flat_entry, song_from_full_info):
            song = convert({"id": "abcdefghijk", "duration": 60, "thumbnail": valid})
            self.assertEqual(song.thumbnail_url, valid)
            self.assertTrue(valid_song(asdict(song)))
        for thumbnail in (
            "https://user:secret@i.ytimg.com/image.jpg",
            "https://i.ytimg.com:8443/image.jpg",
            "https://i.ytimg.com./image.jpg",
            "https://i.ytimg.com/im\nage.jpg",
            "https://i.ytimg.com/image\x7f.jpg",
            "http://i.ytimg.com/image.jpg",
            "https://i.ytimg.com.evil.test/image.jpg",
            "https://i.ytimg.com:invalid/image.jpg",
        ):
            self.assertFalse(valid_thumbnail_url(thumbnail))
            for convert in (song_from_flat_entry, song_from_full_info):
                song = convert({"id": "abcdefghijk", "duration": 60, "thumbnail": thumbnail})
                self.assertEqual(song.thumbnail_url, "")
                self.assertTrue(valid_song(asdict(song)))

    def test_queue_store_rejects_duplicate_entry_and_video_identities(self):
        from core.utils import Song

        key = "0" * 24
        song = Song(
            "abcdefghijk", "Track", "Artist", 60, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        row = {
            "id": "a" * 32,
            "song": asdict(song),
            "owner": "f" * 64,
            "requested_by": "User",
            "added_at": 1,
        }
        payload = {
            "schema_version": 1,
            "sessions": {key: {"umo": "qq:GroupMessage:123", "label": "Group", "group_id": "123"}},
            "queues": {key: [row]},
            "paused": [],
        }
        self.assertTrue(valid_state(payload))
        payload["queues"][key].append({**row, "id": "b" * 32})
        self.assertFalse(valid_state(payload))
        other = Song(
            "bcdefghijkl", "Other", "Artist", 60, "https://www.youtube.com/watch?v=bcdefghijkl"
        )
        payload["queues"][key][1] = {**row, "song": asdict(other)}
        self.assertFalse(valid_state(payload))
        payload["queues"][key][1]["id"] = "b" * 32
        self.assertTrue(valid_state(payload))


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_and_timed_out_waiters_leave_no_admission_leaks(self):
        limiter = JobLimiter(1, 1)
        async with limiter.slot(wait_timeout=1):

            async def wait():
                async with limiter.slot(wait_timeout=0.02):
                    pass

            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            with self.assertRaises(QueueFull):
                async with limiter.slot(wait_timeout=1):
                    pass
            with self.assertRaises(QueueWaitTimeout):
                await waiter
        self.assertEqual(limiter.active, 0)
        self.assertFalse(limiter._waiters)
