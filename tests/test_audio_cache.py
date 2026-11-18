"""Only complete current metadata is reusable; unrelated files are untouched."""

import json
import tempfile
import unittest
from pathlib import Path

from filesystem_helpers import directory_link

from core.audio_cache import AudioCache
from core.utils import Song


class AudioCacheTests(unittest.TestCase):
    def seed(self, root: Path, directory: Path | None = None):
        cache = AudioCache(directory or root / "cache")
        source = root / "source.wav"
        source.write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"x" * 60)
        song = Song(
            "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        self.assertIsNotNone(cache.store(song, source, {"duration_seconds": 1}))
        return cache, source, song

    def test_missing_current_fields_are_not_filled_from_old_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = AudioCache(root / "cache")
            source = root / "source.wav"
            source.write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"x" * 60)
            song = Song(
                "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
            )
            self.assertIsNotNone(cache.store(song, source, {"duration_seconds": 1}))
            self.assertIsNotNone(cache.lookup(song.video_id))
            wav, meta = cache._paths(song.video_id)
            for field in ("last_used_at", "media", "owner"):
                self.assertIsNotNone(cache.store(song, source, {"duration_seconds": 1}))
                payload = json.loads(meta.read_text(encoding="utf-8"))
                payload.pop(field)
                meta.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(cache.lookup(song.video_id))
                self.assertFalse(wav.exists())
                self.assertFalse(meta.exists())

    def test_cleanup_does_not_remove_unowned_or_legacy_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = AudioCache(root)
            files = [root / "old-video.wav", root / "old-video.json", root / "notes.json"]
            for file in files:
                file.write_text("unrelated", encoding="utf-8")
            cache.cleanup()
            cache.clear_unprotected()
            self.assertTrue(all(file.exists() for file in files))

    def test_mismatched_metadata_is_excluded_and_only_its_pair_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            other = Song(
                "bcdefghijkl", "Other", "Artist", 1, "https://www.youtube.com/watch?v=bcdefghijkl"
            )
            self.assertIsNotNone(cache.store(other, source, {"duration_seconds": 1}))
            wav, meta = cache._paths(song.video_id)
            other_wav, other_meta = cache._paths(other.video_id)
            before = (wav.read_bytes(), meta.read_bytes())
            other_meta.write_bytes(meta.read_bytes())
            self.assertEqual([row["video_id"] for row in cache.list_entries()], [song.video_id])
            result = cache.cleanup({song.video_id})
            self.assertEqual(result["removed_expired"], 1)
            self.assertFalse(other_wav.exists())
            self.assertFalse(other_meta.exists())
            self.assertEqual((wav.read_bytes(), meta.read_bytes()), before)

    def test_invalid_media_does_not_overwrite_an_existing_cache_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            wav, meta = cache._paths(song.video_id)
            before = (wav.read_bytes(), meta.read_bytes())
            source.write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"new" * 30)
            for media in (
                {"duration_seconds": float("nan")},
                {"unsupported": object()},
                {"oversized": "x" * (32 * 1024)},
            ):
                with self.subTest(media=next(iter(media))):
                    self.assertIsNone(cache.store(song, source, media))
                    self.assertEqual((wav.read_bytes(), meta.read_bytes()), before)
                    self.assertFalse(list(cache.directory.glob(".*.tmp")))

    def test_ambiguous_and_deeply_nested_metadata_is_invalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            wav, meta = cache._paths(song.video_id)
            valid = meta.read_text(encoding="utf-8")
            for index, raw in enumerate(
                (
                    valid.replace('"owner":', '"owner":"duplicate","owner":', 1),
                    valid.replace('"duration_seconds":1', '"duration_seconds":NaN'),
                    valid.replace('"duration_seconds":1', '"duration_seconds":1e309'),
                    valid.replace(
                        '"duration_seconds":1',
                        '"duration_seconds":' + "[" * 3000 + "0" + "]" * 3000,
                    ),
                )
            ):
                with self.subTest(case=index):
                    self.assertNotEqual(raw, valid)
                    self.assertIsNotNone(cache.store(song, source, {"duration_seconds": 1}))
                    meta.write_text(raw, encoding="utf-8")
                    self.assertEqual(cache.list_entries(), [])
                    self.assertIsNone(cache.lookup(song.video_id))
                    self.assertFalse(wav.exists())
                    self.assertFalse(meta.exists())

    def test_cache_root_directory_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            with directory_link(root / "cache", target) as link:
                with self.assertRaises(ValueError):
                    AudioCache(link)
                self.assertFalse(list(target.iterdir()))

    def test_parent_link_replacement_blocks_all_cache_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root, root / "managed" / "audio")
            original = root / "original"
            cache.directory.parent.rename(original)
            target = root / "target"
            target.mkdir()
            foreign, _, _ = self.seed(root, target / "audio")
            before = {path.name: path.read_bytes() for path in foreign.directory.iterdir()}
            with directory_link(root / "managed", target):
                self.assertIsNone(cache.lookup(song.video_id))
                self.assertIsNone(cache.store(song, source, {"duration_seconds": 1}))
                self.assertEqual(cache.list_entries(), [])
                self.assertFalse(cache.delete(song.video_id))
                self.assertEqual(cache.clear_unprotected(), {"deleted": 0, "skipped_busy": 0})
                self.assertEqual(
                    cache.cleanup(),
                    {"removed_expired": 0, "removed_capacity": 0, "total_bytes": 0, "items": 0},
                )
                self.assertEqual(
                    {path.name: path.read_bytes() for path in foreign.directory.iterdir()}, before
                )
            self.assertTrue(list((original / "audio").glob("*.wav")))

    def test_capacity_eviction_preserves_protected_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            cache.max_size_bytes = source.stat().st_size
            cache.protected_video_ids = {song.video_id}
            other = Song(
                "bcdefghijkl", "Other", "Artist", 1, "https://www.youtube.com/watch?v=bcdefghijkl"
            )
            self.assertIsNone(cache.store(other, source, {"duration_seconds": 1}))
            self.assertIsNotNone(cache.lookup(song.video_id))
            self.assertIsNone(cache.lookup(other.video_id))
