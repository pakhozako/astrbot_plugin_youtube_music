"""Bounded capture uses real local Python processes, never a shell."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.errors import friendly_process_error
from core.process import (
    ProcessCaptureLimit,
    ProcessOutputLimit,
    ProcessTimeout,
    _kill_process_tree,
    run_process,
)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdout_and_stderr_share_capture_limit(self):
        with self.assertRaises(ProcessCaptureLimit) as caught:
            await run_process(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import sys,time; sys.stdout.write('x'*40000); sys.stderr.write('y'*40000); time.sleep(30)",
                ],
                timeout=5,
                max_capture_bytes=65536,
            )
        self.assertEqual(caught.exception.max_bytes, 65536)
        self.assertIn("输出超过", friendly_process_error(caught.exception, "媒体检查"))
        self.assertNotIn("下载文件", friendly_process_error(caught.exception, "媒体检查"))

    async def test_small_nonstreaming_output_is_preserved(self):
        result = await run_process(
            [
                sys.executable,
                "-c",
                "import sys; print('metadata'); print('warning', file=sys.stderr)",
            ],
            timeout=5,
            max_capture_bytes=1024,
        )
        self.assertEqual(result.stdout, "metadata")
        self.assertEqual(result.stderr, "warning")

    async def test_repeated_cancellation_cannot_abandon_process_tree_termination(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def terminate(_process, _job):
            entered.set()
            await release.wait()
            finished.set()

        with patch("core.process._terminate_process_tree", side_effect=terminate):
            task = asyncio.create_task(_kill_process_tree(None))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())

    async def test_real_timeout_waits_for_process_exit(self):
        processes = []
        create = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            return process

        with patch("core.process.asyncio.create_subprocess_exec", side_effect=spawn):
            with self.assertRaises(ProcessTimeout):
                await run_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2
                )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    async def test_real_cancellation_waits_for_process_exit(self):
        ready = asyncio.Event()
        processes = []
        create = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            return process

        def output(line, _stream):
            if line == "ready":
                ready.set()

        with patch("core.process.asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(
                run_process(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ],
                    timeout=5,
                    on_output_line=output,
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), timeout=4)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    async def test_streaming_size_limit_waits_for_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            processes = []
            create = asyncio.create_subprocess_exec

            async def spawn(*args, **kwargs):
                process = await create(*args, **kwargs)
                processes.append(process)
                return process

            with patch("core.process.asyncio.create_subprocess_exec", side_effect=spawn):
                with self.assertRaises(ProcessOutputLimit):
                    await run_process(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'x'*2048); time.sleep(30)",
                            str(source),
                        ],
                        timeout=5,
                        monitor_directory=root,
                        max_directory_size_bytes=1024,
                    )
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].returncode)
            self.assertEqual(source.stat().st_size, 2048)
