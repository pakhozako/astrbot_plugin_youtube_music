"""Ownership checks stay bounded and never authorize deleting directory links."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from filesystem_helpers import directory_link

from core.utils import (
    cleanup_stale_owned_job_dirs,
    is_owned_job_dir,
    mark_owned_job_dir,
)

MARKER = ".astrbot-youtube-music-job"
VALUE = b"astrbot_plugin_youtube_music\n"


class JobDirectorySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_owned_directory_requires_an_exact_marker(self):
        path = self.root / "job-owned"
        self.assertFalse(is_owned_job_dir(path))
        path.mkdir()
        self.assertFalse(is_owned_job_dir(path))
        mark_owned_job_dir(path)
        self.assertTrue(is_owned_job_dir(path))
        marker = path / MARKER
        marker.write_bytes(VALUE.replace(b"\n", b"\r\n"))
        self.assertTrue(is_owned_job_dir(path))
        for payload in (b"", b"other-plugin\n", VALUE.rstrip(), VALUE + b"x", b"\xff"):
            with self.subTest(payload=payload):
                marker.write_bytes(payload)
                self.assertFalse(is_owned_job_dir(path))

    def test_marker_read_is_bounded_and_os_errors_are_false(self):
        path = self.root / "job-owned"
        path.mkdir()
        mark_owned_job_dir(path)
        stream = MagicMock()
        stream.read.return_value = VALUE
        with patch.object(Path, "open") as opening:
            opening.return_value.__enter__.return_value = stream
            self.assertTrue(is_owned_job_dir(path))
        stream.read.assert_called_once_with(len(VALUE) + 2)
        with patch.object(Path, "open", side_effect=OSError("cannot read marker")):
            self.assertFalse(is_owned_job_dir(path))

    def test_directory_link_and_directory_marker_are_rejected(self):
        target = self.root / "owned-target"
        target.mkdir()
        mark_owned_job_dir(target)
        with directory_link(self.root / "job-link", target) as link:
            self.assertFalse(is_owned_job_dir(link))
        marker_dir = self.root / "job-marker-dir"
        marker_dir.mkdir()
        marker_target = marker_dir / "owned-target"
        marker_target.mkdir()
        mark_owned_job_dir(marker_target)
        with directory_link(marker_dir / MARKER, marker_target):
            self.assertFalse(is_owned_job_dir(marker_dir))
        (marker_dir / MARKER).mkdir()
        self.assertFalse(is_owned_job_dir(marker_dir))

    def test_file_marker_symlink_is_rejected_without_reading_the_target(self):
        path = self.root / "job-owned"
        path.mkdir()
        target = self.root / "marker-target"
        target.write_bytes(VALUE)
        try:
            (path / MARKER).symlink_to(target)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {type(exc).__name__}")
        with patch.object(Path, "open", side_effect=AssertionError("must not read a symlink")):
            self.assertFalse(is_owned_job_dir(path))
        self.assertEqual(target.read_bytes(), VALUE)

    def test_stale_cleanup_removes_only_owned_direct_jobs(self):
        cutoff = time.time() - 60
        old = self.root / "job-old"
        fresh = self.root / "job-fresh"
        unknown = self.root / "job-unknown"
        unrelated = self.root / "unrelated"
        for path in (old, fresh, unknown, unrelated):
            path.mkdir()
            (path / "source.webm").write_bytes(b"data")
        for path in (old, fresh, unrelated):
            mark_owned_job_dir(path)
        for path in (old, unknown, unrelated):
            os.utime(path, (cutoff - 60, cutoff - 60))
        self.assertEqual(cleanup_stale_owned_job_dirs(self.root, cutoff), 1)
        self.assertFalse(old.exists())
        for path in (fresh, unknown, unrelated):
            self.assertEqual((path / "source.webm").read_bytes(), b"data")

    def test_stale_cleanup_does_not_follow_root_or_job_links(self):
        target = self.root / "owned-target"
        target.mkdir()
        nested = target / "job-nested"
        nested.mkdir()
        mark_owned_job_dir(target)
        mark_owned_job_dir(nested)
        os.utime(target, (1, 1))
        os.utime(nested, (1, 1))
        with directory_link(self.root / "job-link", target) as link:
            self.assertEqual(cleanup_stale_owned_job_dirs(link, time.time()), 0)
            self.assertEqual(cleanup_stale_owned_job_dirs(self.root, time.time()), 0)
            self.assertTrue(nested.is_dir())
            self.assertTrue(is_owned_job_dir(target))
