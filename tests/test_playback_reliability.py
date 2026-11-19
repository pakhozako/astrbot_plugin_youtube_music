"""Private playback files, bounded retries and post-submission cancellation regressions."""

import asyncio
import copy
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from filesystem_helpers import directory_link

from core.audio_cache import AudioCache
from core.errors import UserFacingError, friendly_process_error, transient_network_error
from core.maintenance import MaintenanceGate, run_blocking
from core.process import ProcessError, ProcessOutputLimit, ProcessResult, ProcessTimeout
from core.service import YouTubeMusicService
from core.settings import Settings
from core.utils import Song


def track(duration=1, video_id="abcdefghijk"):
    return Song(
        video_id, "Track", "Artist", duration, f"https://www.youtube.com/watch?v={video_id}"
    )


def wav_probe(duration=1):
    return {
        "duration": duration,
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
    }


class PlaybackReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def service(self, root, cache=None):
        service = YouTubeMusicService(Settings.from_config({}, data_dir=root), cache)
        service.dependency_gate = MaintenanceGate()
        return service

    def seed(self, root):
        cache = AudioCache(root / "audio")
        source = root / "source.wav"
        source.write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"x" * 1000)
        song = track()
        self.assertIsNotNone(cache.store(song, source, {"duration_seconds": 1}))
        return cache, source, song

    async def test_preflight_rejects_unknown_duration_before_cache_or_network(self):
        service = self.service(Path(tempfile.gettempdir()))
        service._validated_cache_entry = AsyncMock()
        service.get_metadata = AsyncMock()
        service._run_ytdlp = AsyncMock()
        for duration in (None, 0, -1, True, 999):
            with self.subTest(duration=duration):
                with self.assertRaises(UserFacingError):
                    await service.download_and_convert(track(duration), Path("not-created"))
                with self.assertRaises(UserFacingError):
                    await service._download_resolved(
                        track(duration),
                        Path("not-created"),
                        on_output_line=None,
                        on_stage=None,
                    )
        service._validated_cache_entry.assert_not_awaited()
        service.get_metadata.assert_not_awaited()
        service._run_ytdlp.assert_not_awaited()

    async def test_wav_budget_is_checked_before_any_download_but_not_for_links(self):
        service = self.service(Path(tempfile.gettempdir()))
        service.settings = replace(service.settings, max_wav_size_mb=1)
        song = track(60)
        self.assertEqual(service.preflight(song, voice=False), 180)
        with self.assertRaisesRegex(UserFacingError, "WAV"):
            service.preflight(song)
        with self.assertRaises(UserFacingError):
            service.preflight(track(16), max_duration_seconds=15, voice=False)
        service.get_metadata = AsyncMock()
        service._run_ytdlp = AsyncMock()
        with self.assertRaisesRegex(UserFacingError, "WAV"):
            await service.download_and_convert(song, Path("not-created"))
        with self.assertRaisesRegex(UserFacingError, "WAV"):
            await service._download_resolved(
                song, Path("not-created"), on_stage=None, on_output_line=None
            )
        service.get_metadata.assert_not_awaited()
        service._run_ytdlp.assert_not_awaited()

    async def test_other_group_cache_invalidation_cannot_delete_private_playback_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            job_a, job_b = root / "job-a", root / "job-b"
            job_a.mkdir()
            job_b.mkdir()
            service = self.service(root, cache)
            service._probe = AsyncMock(side_effect=[wav_probe(), {"duration": 1, "streams": []}])
            service.get_metadata = AsyncMock(side_effect=UserFacingError("unavailable"))
            first, returned_song, media = await service.download_and_convert(song, job_a)
            self.assertEqual(first, job_a / "record.wav")
            self.assertEqual(returned_song, song)
            self.assertTrue(media["cache_hit"])
            self.assertNotEqual(first, cache._paths(song.video_id)[0])
            with self.assertRaisesRegex(UserFacingError, "unavailable"):
                await service.download_and_convert(song, job_b)
            self.assertIsNone(cache.lookup(song.video_id))
            self.assertEqual(first.read_bytes(), source.read_bytes())

    async def test_transient_probe_failure_preserves_shared_cache_and_other_private_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            job_a, job_b = root / "job-a", root / "job-b"
            job_a.mkdir()
            job_b.mkdir()
            error = UserFacingError("temporary probe timeout")
            error.__cause__ = ProcessTimeout("probe timeout")
            service = self.service(root, cache)
            service._probe = AsyncMock(side_effect=[wav_probe(), error])
            service.get_metadata = AsyncMock()
            first, _, _ = await service.download_and_convert(song, job_a)
            with self.assertRaisesRegex(UserFacingError, "temporary probe"):
                await service.download_and_convert(song, job_b)
            self.assertIsNotNone(cache.lookup(song.video_id))
            self.assertEqual(first.read_bytes(), source.read_bytes())
            service.get_metadata.assert_not_awaited()

    async def test_fresh_download_stores_cache_but_returns_private_job_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = AudioCache(root / "audio")
            job = root / "job"
            job.mkdir()
            service = self.service(root, cache)
            song = track()

            async def download(*_args, **_kwargs):
                (job / "source.webm").write_bytes(b"source" * 20)

            async def convert(*_args, **_kwargs):
                (job / "record.wav").write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"x" * 1000)

            service._run_ytdlp = AsyncMock(side_effect=download)
            service._probe = AsyncMock(side_effect=[wav_probe(), wav_probe()])
            with patch("core.service.run_process", new=AsyncMock(side_effect=convert)):
                path, resolved, media = await service._download_resolved(
                    song, job, on_stage=None, on_output_line=None
                )
            self.assertEqual(path, job / "record.wav")
            self.assertEqual(resolved, song)
            self.assertTrue(media["cache_stored"])
            cached = cache.lookup(song.video_id)
            self.assertIsNotNone(cached)
            self.assertNotEqual(cached.path, path)
            self.assertEqual(cached.path.read_bytes(), path.read_bytes())

    async def test_cache_lock_covers_snapshot_copy_until_private_file_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            job = root / "job"
            job.mkdir()
            started, release = threading.Event(), threading.Event()
            original_replace = os.replace

            def replace_file(source_path, target_path):
                if Path(target_path) == job / "record.wav":
                    started.set()
                    release.wait(3)
                return original_replace(source_path, target_path)

            with patch("core.audio_cache.os.replace", side_effect=replace_file):
                copy_task = asyncio.create_task(
                    run_blocking(cache.copy_for_job, song.video_id, job)
                )
                delete_task = None
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    delete_task = asyncio.create_task(
                        asyncio.to_thread(cache.invalidate, song.video_id)
                    )
                    await asyncio.sleep(0.03)
                    self.assertFalse(delete_task.done())
                finally:
                    release.set()
                    entry = await copy_task
                    if delete_task is not None:
                        await delete_task
            self.assertEqual(entry.path.read_bytes(), source.read_bytes())
            self.assertIsNone(cache.lookup(song.video_id))
            self.assertFalse(list(job.glob("*.tmp")))

    async def test_cancelled_snapshot_finishes_before_job_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            job = root / "job"
            job.mkdir()
            started, release = threading.Event(), threading.Event()
            original_replace = os.replace

            def replace_file(source_path, target_path):
                if Path(target_path) == job / "record.wav":
                    started.set()
                    release.wait(3)
                return original_replace(source_path, target_path)

            with patch("core.audio_cache.os.replace", side_effect=replace_file):
                task = asyncio.create_task(run_blocking(cache.copy_for_job, song.video_id, job))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    task.cancel()
                    await asyncio.sleep(0.03)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual((job / "record.wav").read_bytes(), source.read_bytes())
            self.assertFalse(list(job.glob("*.tmp")))

    async def test_snapshot_destination_and_capacity_are_validated_without_touching_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, source, song = self.seed(root)
            job = root / "job"
            job.mkdir()
            with self.assertRaises(ValueError):
                cache.copy_for_job(song.video_id, cache.directory)
            (job / "record.wav").mkdir()
            with self.assertRaises(ValueError):
                cache.copy_for_job(song.video_id, job)
            (job / "record.wav").rmdir()
            self.assertIsNone(cache.copy_for_job(song.video_id, job, max_size_bytes=100))
            self.assertFalse((job / "record.wav").exists())
            self.assertIsNotNone(cache.lookup(song.video_id))
            self.assertEqual(cache.lookup(song.video_id).path.read_bytes(), source.read_bytes())
            target = root / "target"
            target.mkdir()
            with directory_link(root / "linked-job", target) as linked:
                with self.assertRaises(ValueError):
                    cache.copy_for_job(song.video_id, linked)
                self.assertFalse(list(target.iterdir()))

    async def test_explicit_temporary_network_errors_are_retried_once(self):
        service = self.service(Path(tempfile.gettempdir()))
        for message in (
            "Connection reset by peer",
            "Temporary failure in name resolution",
            "HTTP Error 503: Service Unavailable",
        ):
            with self.subTest(message=message):
                process = AsyncMock(
                    side_effect=[ProcessError(message), ProcessResult((), 0, "{}", "")]
                )
                with (
                    patch("core.service.run_process", new=process),
                    patch("core.service.asyncio.sleep", new=AsyncMock()) as sleep,
                ):
                    result = await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
                self.assertEqual(result.stdout, "{}")
                self.assertEqual(process.await_count, 2)
                sleep.assert_awaited_once_with(0.5)
                first, second = process.await_args_list
                self.assertLessEqual(second.kwargs["timeout"], first.kwargs["timeout"])

    async def test_retry_shares_one_stage_deadline_instead_of_resetting_timeout(self):
        service = self.service(Path(tempfile.gettempdir()))
        process = AsyncMock(
            side_effect=[ProcessError("Connection reset"), ProcessResult((), 0, "{}", "")]
        )
        loop = Mock()
        loop.time.side_effect = [100, 100.1, 102, 106]
        with (
            patch("core.service.asyncio.get_running_loop", return_value=loop),
            patch("core.service.asyncio.sleep", new=AsyncMock()),
            patch("core.service.run_process", new=process),
        ):
            await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
        self.assertAlmostEqual(process.await_args_list[0].kwargs["timeout"], 9.9)
        self.assertEqual(process.await_args_list[1].kwargs["timeout"], 4)

    async def test_expired_stage_deadline_does_not_launch_another_process(self):
        service = self.service(Path(tempfile.gettempdir()))
        process = AsyncMock(side_effect=ProcessError("Connection reset"))
        loop = Mock()
        loop.time.side_effect = [100, 100.1, 102, 111]
        with (
            patch("core.service.asyncio.get_running_loop", return_value=loop),
            patch("core.service.asyncio.sleep", new=AsyncMock()),
            patch("core.service.run_process", new=process),
        ):
            with self.assertRaises(UserFacingError) as failure:
                await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
        self.assertIsInstance(failure.exception.__cause__, ProcessTimeout)
        process.assert_awaited_once()

    async def test_policy_format_timeout_size_and_unknown_errors_are_never_auto_retried(self):
        service = self.service(Path(tempfile.gettempdir()))
        failures = [
            ProcessTimeout("timeout"),
            ProcessOutputLimit("size limit", actual_bytes=100, max_bytes=1),
            *[
                ProcessError(message)
                for message in (
                    "HTTP Error 429: Too Many Requests",
                    "Sign in to confirm your age",
                    "Private video",
                    "Not available in your country",
                    "Requested format is not available",
                    "This video is unavailable; HTTP Error 503",
                    "Video is not available; Connection reset",
                    "Blocked in your country; HTTP Error 503",
                    "JS challenge solver failed; Connection reset",
                    "Unexpected extractor failure",
                )
            ],
        ]
        for error in failures:
            with self.subTest(error=str(error)):
                self.assertFalse(transient_network_error(error))
                with (
                    patch("core.service.run_process", new=AsyncMock(side_effect=error)) as process,
                    patch("core.service.asyncio.sleep", new=AsyncMock()) as sleep,
                ):
                    with self.assertRaises(UserFacingError):
                        await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
                process.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_cancellation_is_not_wrapped_or_retried(self):
        service = self.service(Path(tempfile.gettempdir()))
        with (
            patch(
                "core.service.run_process", new=AsyncMock(side_effect=asyncio.CancelledError)
            ) as process,
            patch("core.service.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
        process.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_failed_retry_is_bounded_and_does_not_disclose_proxy_secrets(self):
        service = self.service(Path(tempfile.gettempdir()))
        error = ProcessError("HTTP Error 503 https://user:secret@proxy/?token=private")
        with (
            patch("core.service.run_process", new=AsyncMock(side_effect=error)) as process,
            patch("core.service.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(UserFacingError) as failure:
                await service._run_ytdlp(["ytsearch3:Track"], 10, "search")
        self.assertEqual(process.await_count, 2)
        self.assertNotIn("secret", str(failure.exception))
        self.assertNotIn("private", str(failure.exception))

    def test_unavailable_format_is_not_reported_as_a_deleted_video(self):
        error = ProcessError("Requested format is not available. Use --list-formats.")
        message = friendly_process_error(error, "download")
        self.assertIn("yt-dlp", message)
        self.assertNotIn("\u89c6\u9891\u4e0d\u53ef\u7528", message)


class SubmittedQueueReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_runtime as runtime

        self.runtime = runtime
        await runtime.RuntimeTests.asyncSetUp(self)

    async def add(self, **kwargs):
        return await self.runtime.RuntimeTests.add(self, **kwargs)

    async def wait_for(self, predicate):
        await self.runtime.RuntimeTests.wait_for(self, predicate)

    def voice_submission_count(self):
        return sum(
            any(
                isinstance(component, self.runtime.module.Comp.Record)
                for component in getattr(call.args[1], "chain", [])
            )
            for call in self.context.send_message.await_args_list
        )

    async def test_pause_during_finalization_keeps_history_for_already_submitted_voice(self):
        await self.add()
        original_update = self.plugin.state.update
        started, release = threading.Event(), threading.Event()

        def update(mutate):
            before = self.plugin.state.snapshot()
            candidate = copy.deepcopy(before)
            mutate(candidate)
            if before["queues"].get(self.key) and not candidate["queues"].get(self.key):
                started.set()
                release.wait(3)
            return original_update(mutate)

        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        with patch.object(self.plugin.state, "update", side_effect=update):
            self.plugin._start_worker(self.key)
            pause = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                worker = self.plugin._workers[self.key]
                pause = asyncio.create_task(self.plugin.queue_action(self.key, "pause", admin=True))
                await self.wait_for(lambda: worker.cancelling() > 0)
            finally:
                release.set()
                if pause is not None:
                    await pause
        self.assertEqual(self.voice_submission_count(), 1)
        self.assertEqual(self.plugin.library.history(self.owner)[0]["play_count"], 1)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertTrue(worker.cancelled())

    async def pause_with_failed_finalization(self, stage):
        await self.add()
        original_update = self.plugin.state.update
        original_history = self.plugin.library.add_history_for_owner_id
        started, release = threading.Event(), threading.Event()

        def fail_write():
            started.set()
            release.wait(3)
            raise OSError(f"{stage} write failed")

        def update(mutate):
            before = self.plugin.state.snapshot()
            candidate = copy.deepcopy(before)
            mutate(candidate)
            if (
                stage == "queue"
                and before["queues"].get(self.key)
                and not candidate["queues"].get(self.key)
            ):
                fail_write()
            return original_update(mutate)

        def history(owner_id, song):
            if stage == "history":
                fail_write()
            return original_history(owner_id, song)

        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        with (
            patch.object(self.plugin.state, "update", side_effect=update),
            patch.object(self.plugin.library, "add_history_for_owner_id", side_effect=history),
        ):
            row = self.plugin.state.snapshot("queues", self.key)[0]
            self.plugin._start_worker(self.key)
            pause = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                worker = self.plugin._workers[self.key]
                pause = asyncio.create_task(self.plugin.queue_action(self.key, "pause", admin=True))
                await self.wait_for(lambda: worker.cancelling() > 0)
                self.assertFalse(pause.done())
            finally:
                release.set()
                if pause is not None:
                    await pause

        self.assertEqual(self.voice_submission_count(), 1)
        self.assertFalse(self.plugin.library.history(self.owner))
        self.assertIn("\u6d88\u606f\u5df2\u63d0\u4ea4", self.plugin._errors[self.key])
        self.assertEqual(self.plugin._error_entry_ids[self.key], row["id"])
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertFalse(worker.cancelled())
        return row

    async def test_pause_during_failed_queue_finalization_keeps_submission_risk(self):
        row = await self.pause_with_failed_finalization("queue")
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [row])

    async def test_pause_during_failed_history_finalization_keeps_submission_risk(self):
        await self.pause_with_failed_finalization("history")
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))

    async def test_successful_voice_cleanup_failure_does_not_pause_or_replay_group_queue(self):
        await self.add()
        await self.add(song=self.runtime.workflow_song(1))
        original_rmtree = self.runtime.module.shutil.rmtree

        def cleanup(path, *args, **kwargs):
            if Path(path).parent == self.plugin.job_root:
                if kwargs.get("ignore_errors"):
                    return None
                raise OSError("temporary file lock")
            return original_rmtree(path, *args, **kwargs)

        self.plugin.service.download_and_convert = AsyncMock(
            side_effect=lambda song, *_args, **_kwargs: (self.root / "track.wav", song, {})
        )
        with patch("youtube_music_test.main.shutil.rmtree", side_effect=cleanup):
            self.plugin._start_worker(self.key)
            await self.wait_for(
                lambda: (
                    self.key in self.plugin._errors
                    or len(self.plugin.library.history(self.owner)) == 2
                )
            )
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertEqual(len(self.plugin.library.history(self.owner)), 2)
        self.assertEqual(self.voice_submission_count(), 2)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertNotIn(self.key, self.plugin.state.snapshot("paused"))
        await self.plugin.queue_action(self.key, "pause", admin=True)

    async def test_job_cleanup_rejects_parent_directory_links(self):
        jobs = self.plugin.job_root
        target = jobs.parent / "redirected-jobs"
        protected = target / f"job-{'1' * 32}"
        protected.mkdir(parents=True)
        self.runtime.module.mark_owned_job_dir(protected)
        sentinel = protected / "sentinel.wav"
        sentinel.write_bytes(b"must remain untouched")
        self.assertTrue(jobs.resolve().is_relative_to(self.root.resolve()))
        jobs.rmdir()
        try:
            with directory_link(jobs, target):
                self.plugin._cleanup_job(jobs / protected.name)
                self.assertEqual(sentinel.read_bytes(), b"must remain untouched")
        finally:
            jobs.mkdir(exist_ok=True)

    async def test_job_creation_rejects_parent_directory_links(self):
        jobs = self.plugin.job_root
        target = jobs.parent / "redirected-jobs"
        target.mkdir()
        self.assertTrue(jobs.resolve().is_relative_to(self.root.resolve()))
        jobs.rmdir()
        name = f"job-{'2' * 32}"
        try:
            with directory_link(jobs, target):
                with self.assertRaises((OSError, ValueError, self.runtime.module.UserFacingError)):
                    self.plugin._create_job(jobs / name)
                self.assertFalse((target / name).exists())
        finally:
            jobs.mkdir(exist_ok=True)


if __name__ == "__main__":
    unittest.main()
