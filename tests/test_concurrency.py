"""Cancellation, maintenance and cache-lock regressions."""

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.maintenance import MaintenanceGate, MaintenanceUnavailable, run_blocking
from core.queue import JobLimiter, QueueWaitTimeout
from core.service import YouTubeMusicService
from core.settings import Settings
from core.utils import Song


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        settings = Settings.from_config({}, data_dir=Path(tempfile.gettempdir()) / "youtube-test")
        service = YouTubeMusicService(settings)
        service.dependency_gate = MaintenanceGate()
        return service

    async def cancel_while_exit_lock_is_held(self, condition, task):
        await condition.acquire()
        try:
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            self.assertFalse(task.done(), "Cancellation must wait for locked cleanup")
        finally:
            condition.release()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_repeated_slot_cancellation_releases_capacity_and_preserves_fifo(self):
        limiter = JobLimiter(max_concurrent=1, max_queued=2)
        entered = asyncio.Event()
        order = []

        async def hold():
            async with limiter.slot(wait_timeout=2):
                entered.set()
                await asyncio.Future()

        async def wait(label):
            async with limiter.slot(wait_timeout=2):
                order.append(label)
                self.assertEqual(limiter.active, 1)
                await asyncio.sleep(0)

        holder = asyncio.create_task(hold())
        await entered.wait()
        first = asyncio.create_task(wait("first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(wait("second"))
        await asyncio.sleep(0)
        try:
            await self.cancel_while_exit_lock_is_held(limiter._condition, holder)
            await asyncio.gather(first, second)
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(limiter.active, 0)
        self.assertFalse(limiter._waiters)
        async with limiter.slot(wait_timeout=0.1):
            self.assertEqual(limiter.active, 1)
        self.assertEqual(limiter.active, 0)

    async def test_repeated_use_cancellation_releases_original_holder_and_restores_work(self):
        gate = MaintenanceGate()
        entered = asyncio.Event()

        async def hold():
            async with gate.use():
                entered.set()
                await asyncio.Future()

        holder = asyncio.create_task(hold())
        await entered.wait()
        self.assertEqual(gate._holders, {holder: 1})
        await self.cancel_while_exit_lock_is_held(gate._condition, holder)
        self.assertFalse(gate._holders)
        self.assertFalse(gate.task_snapshot())
        async with gate.maintenance(timeout=0.1):
            self.assertTrue(gate.maintaining)
        async with gate.use(timeout=0.1):
            self.assertEqual(gate._holders, {asyncio.current_task(): 1})
        self.assertFalse(gate.task_snapshot())

    async def test_repeated_nested_use_cancellation_releases_every_original_holder_depth(self):
        gate = MaintenanceGate()
        owner = asyncio.current_task()
        async with gate.use():
            self.assertEqual(gate._holders, {owner: 1})
            async with gate.use():
                self.assertEqual(gate._holders, {owner: 2})
            self.assertEqual(gate._holders, {owner: 1})
        self.assertFalse(gate._holders)
        entered = asyncio.Event()

        async def hold():
            async with gate.use():
                async with gate.use():
                    entered.set()
                    await asyncio.Future()

        holder = asyncio.create_task(hold())
        await entered.wait()
        self.assertEqual(gate._holders, {holder: 2})
        await self.cancel_while_exit_lock_is_held(gate._condition, holder)
        self.assertFalse(gate.task_snapshot())
        async with gate.maintenance(timeout=0.1):
            self.assertTrue(gate.maintaining)
        self.assertFalse(gate.maintaining)

    async def test_repeated_maintenance_cancellation_releases_exclusive_update_and_restores_work(
        self,
    ):
        gate = MaintenanceGate()
        entered = asyncio.Event()

        async def update():
            async with gate.maintenance():
                entered.set()
                await asyncio.Future()

        updater = asyncio.create_task(update())
        await entered.wait()
        self.assertTrue(gate.maintaining)
        self.assertEqual(gate.update_tasks, {updater})
        await self.cancel_while_exit_lock_is_held(gate._condition, updater)
        self.assertFalse(gate.maintaining)
        self.assertFalse(gate.task_snapshot())
        async with gate.use(timeout=0.1):
            self.assertEqual(gate._holders, {asyncio.current_task(): 1})
        async with gate.maintenance(timeout=0.1):
            self.assertEqual(gate.update_tasks, {asyncio.current_task()})
        self.assertFalse(gate.task_snapshot())

    async def test_resource_finalizers_preserve_body_errors(self):
        gate = MaintenanceGate()
        limiter = JobLimiter(max_concurrent=1, max_queued=1)
        with self.assertRaisesRegex(RuntimeError, "slot failed"):
            async with limiter.slot(wait_timeout=0.1):
                raise RuntimeError("slot failed")
        self.assertEqual(limiter.active, 0)
        with self.assertRaisesRegex(RuntimeError, "use failed"):
            async with gate.use():
                raise RuntimeError("use failed")
        self.assertFalse(gate.task_snapshot())
        with self.assertRaisesRegex(RuntimeError, "update failed"):
            async with gate.maintenance():
                raise RuntimeError("update failed")
        self.assertFalse(gate.maintaining)
        self.assertFalse(gate.task_snapshot())

    async def test_admission_timeouts_keep_existing_errors_and_restore_resources(self):
        gate = MaintenanceGate()
        limiter = JobLimiter(max_concurrent=1, max_queued=1)
        async with limiter.slot(wait_timeout=0.1):
            with self.assertRaises(QueueWaitTimeout):
                async with limiter.slot(wait_timeout=0.01):
                    self.fail("A timed-out waiter cannot enter")
            self.assertEqual(limiter.active, 1)
            self.assertFalse(limiter._waiters)
        self.assertEqual(limiter.active, 0)
        async with gate.maintenance():
            with self.assertRaises(MaintenanceUnavailable):
                async with gate.use(timeout=0.01):
                    self.fail("A timed-out dependency user cannot enter")
            self.assertTrue(gate.maintaining)
            self.assertFalse(gate._holders)
        async with gate.use():

            async def update():
                async with gate.maintenance(timeout=0.01):
                    self.fail("A timed-out maintenance task cannot enter")

            with self.assertRaises(TimeoutError):
                await asyncio.create_task(update())
            self.assertFalse(gate.maintaining)
            self.assertFalse(gate.update_tasks)
            self.assertEqual(gate._holders, {asyncio.current_task(): 1})
        self.assertFalse(gate.task_snapshot())

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
