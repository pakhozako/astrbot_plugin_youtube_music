"""Cancellation, maintenance and cache-lock regressions."""

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.maintenance import MaintenanceGate, MaintenanceUnavailable, run_blocking
from core.queue import JobLimiter
from core.service import YouTubeMusicService
from core.settings import Settings
from core.utils import Song


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        settings = Settings.from_config({}, data_dir=Path(tempfile.gettempdir()) / "youtube-test")
        service = YouTubeMusicService(settings)
        service.dependency_gate = MaintenanceGate()
        return service

    async def test_cancel_waits_for_file_thread(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def copy():
            started.set()
            release.wait(2)
            finished.set()

        task = asyncio.create_task(run_blocking(copy))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_cache_lock_reuses_waiter_identity_and_removes_idle_entries(self):
        service = self.service()
        first = service._lock_for("video")
        await first.__aenter__()
        identity = service._cache_locks["video"]

        async def acquire():
            async with service._lock_for("video"):
                self.assertIs(service._cache_locks["video"], identity)
                await asyncio.sleep(0)

        waiter = asyncio.create_task(acquire())
        await asyncio.sleep(0)
        for index in range(1100):
            async with service._lock_for(str(index)):
                pass
        await first.__aexit__(None, None, None)
        newcomer = asyncio.create_task(acquire())
        await asyncio.gather(waiter, newcomer)
        self.assertFalse(service._cache_locks)

    async def test_cancelled_lock_waiter_is_released(self):
        service = self.service()
        async with service._lock_for("video"):

            async def wait():
                async with service._lock_for("video"):
                    pass

            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(service._cache_locks["video"].users, 1)
        self.assertFalse(service._cache_locks)

    async def test_dependency_health_is_tracked_and_closed_gate_rejects_work(self):
        from core.dependency_manager import DependencyManager

        service = self.service()
        manager = DependencyManager(Path(tempfile.gettempdir()), service.settings)
        manager.settings = service.settings
        await manager.dependency_gate.close()
        with patch("core.dependency_manager.run_process", new=AsyncMock()) as process:
            with self.assertRaises(MaintenanceUnavailable):
                await manager.inspect()
            process.assert_not_awaited()

    async def test_maintenance_waits_for_existing_user(self):
        gate = MaintenanceGate()
        async with gate.use():
            entered = asyncio.Event()

            async def update():
                async with gate.maintenance():
                    entered.set()

            task = asyncio.create_task(update())
            await asyncio.sleep(0.02)
            self.assertFalse(entered.is_set())
        await task
        self.assertTrue(entered.is_set())

    async def test_failed_waiting_callback_does_not_leave_a_fifo_ticket(self):
        limiter = JobLimiter(max_concurrent=1, max_queued=1)

        def failed_callback(_phase):
            raise RuntimeError("display failed")

        async with limiter.slot(wait_timeout=1):
            with self.assertRaisesRegex(RuntimeError, "display failed"):
                async with limiter.slot(wait_timeout=1, on_state=failed_callback):
                    self.fail("A failed waiting callback cannot enter the job")
            self.assertFalse(limiter._waiters)
        async with limiter.slot(wait_timeout=0.1):
            self.assertEqual(limiter.active, 1)
        self.assertEqual(limiter.active, 0)

    async def test_real_cache_write_finishes_before_consumer_deletes_source(self):
        service = self.service()
        started, release, copied = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            job.mkdir()
            song = Song(
                "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
            )

            async def download(*args, **kwargs):
                (job / "source.webm").write_bytes(b"audio" * 20)

            async def convert(*args, **kwargs):
                (job / "record.wav").write_bytes(b"RIFF" + b"x" * 4 + b"WAVE" + b"x" * 60)

            def store(_song, source, _media):
                started.set()
                release.wait(2)
                if source.is_file():
                    copied.set()
                return None

            from types import SimpleNamespace

            service.audio_cache = SimpleNamespace(store=store)
            service._run_ytdlp = AsyncMock(side_effect=download)
            service._probe = AsyncMock(
                return_value={
                    "duration": 1,
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "pcm_s16le",
                            "sample_rate": "16000",
                            "channels": 1,
                        }
                    ],
                }
            )

            async def consume():
                try:
                    await service._download_resolved(song, job, on_stage=None, on_output_line=None)
                finally:
                    await run_blocking(shutil.rmtree, job)

            with patch("core.service.run_process", new=AsyncMock(side_effect=convert)):
                task = asyncio.create_task(consume())
                await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertTrue((job / "record.wav").exists())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(copied.is_set())
            self.assertFalse(job.exists())


if __name__ == "__main__":
    unittest.main()
