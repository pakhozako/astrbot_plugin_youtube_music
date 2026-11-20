"""Safe async subprocess execution with progress callbacks and hard size limits."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .windows_job import WindowsJob


@dataclass(slots=True)
class ProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessError(RuntimeError):
    pass


class ProcessExitError(ProcessError):
    """A started process exited unsuccessfully with bounded, structured diagnostics."""

    def __init__(self, *, label: str, returncode: int, diagnostic: str):
        self.returncode = returncode
        self.diagnostic = "\n".join(str(diagnostic).splitlines()[-8:])[-1800:]
        super().__init__(f"{label}退出码为 {returncode}。{self.diagnostic}")


class ProcessTimeout(ProcessError):
    pass


class ProcessOutputLimit(ProcessError):
    def __init__(self, message: str, *, actual_bytes: int, max_bytes: int):
        super().__init__(message)
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


class ProcessCaptureLimit(ProcessOutputLimit):
    """Captured stdout and stderr exceeded the shared memory limit."""


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace").strip()


async def _kill_process_tree(
    proc: asyncio.subprocess.Process, job: WindowsJob | None = None
) -> None:
    cleanup = asyncio.create_task(_terminate_process_tree(proc, job))
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            cancelled = True
            if cleanup.done():
                cleanup.result()
                break
    if cancelled:
        raise asyncio.CancelledError


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process, job: WindowsJob | None = None
) -> None:
    if job is not None:
        job.close()
    elif os.name == "nt":
        if proc.returncode is not None:
            return
        flags = subprocess.CREATE_NO_WINDOW
        killer = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=flags,
            )
            await asyncio.wait_for(killer.wait(), timeout=8)
            if killer.returncode and proc.returncode is None:
                proc.kill()
        except (TimeoutError, OSError):
            if killer is not None and killer.returncode is None:
                killer.kill()
                await killer.wait()
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            pass
        # The parent may have exited while descendants still own its pipes.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError as exc:
            raise ProcessError("无法在退出期限内终止子进程。") from exc


async def _cancel_tasks(tasks) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        while True:
            try:
                await asyncio.shield(cleanup)
                return
            except asyncio.CancelledError:
                if cleanup.done():
                    cleanup.result()
                    return


def _directory_size(directory: Path, pattern: str) -> int:
    total = 0
    try:
        for path in directory.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith((".json", ".ytdl", ".description", ".jpg", ".webp", ".png")):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


async def run_process(
    args: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    label: str = "子进程",
    on_output_line: Callable[[str, str], object] | None = None,
    monitor_directory: Path | None = None,
    monitor_pattern: str = "source.*",
    max_directory_size_bytes: int | None = None,
    max_capture_bytes: int = 8 * 1024 * 1024,
) -> ProcessResult:
    """Execute without a shell, capture output and terminate on timeout/cancel/size limit.

    Non-streaming output is captured under a shared stdout/stderr byte limit.
    For download processes, drain stdout/stderr line-by-line and monitor partial files while
    yt-dlp is running, so size limits are enforced during transfer, not only afterwards.
    """
    if max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be positive")
    command = tuple(str(arg) for arg in args)
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": dict(env) if env is not None else None,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(*command, **kwargs)
    except FileNotFoundError as exc:
        raise ProcessError(f"找不到程序：{command[0]}") from exc
    except OSError as exc:
        raise ProcessError(f"无法启动{label}：{exc}") from exc

    job = None
    if os.name == "nt":
        try:
            job = WindowsJob(proc.pid)
        except OSError as exc:
            await _kill_process_tree(proc)
            raise ProcessError(f"无法隔离{label}的进程树。") from exc

    streaming = on_output_line is not None or (
        monitor_directory is not None and max_directory_size_bytes is not None
    )
    if not streaming:
        captured_bytes = 0

        async def capture(stream):
            nonlocal captured_bytes
            chunks = []
            while chunk := await stream.read(8192):
                captured_bytes += len(chunk)
                if captured_bytes > max_capture_bytes:
                    raise ProcessCaptureLimit(
                        f"{label}输出超过容量上限，已终止进程。",
                        actual_bytes=captured_bytes,
                        max_bytes=max_capture_bytes,
                    )
                chunks.append(chunk)
            return b"".join(chunks)

        capture_tasks = [
            asyncio.create_task(proc.wait()),
            asyncio.create_task(capture(proc.stdout)),
            asyncio.create_task(capture(proc.stderr)),
        ]
        capture_task = asyncio.gather(*capture_tasks)
        try:
            _, stdout_raw, stderr_raw = await asyncio.wait_for(
                asyncio.shield(capture_task), timeout=timeout
            )
        except TimeoutError as exc:
            await _kill_process_tree(proc, job)
            raise ProcessTimeout(f"{label}超过 {timeout:g} 秒仍未完成，已终止进程。") from exc
        except asyncio.CancelledError:
            await _kill_process_tree(proc, job)
            raise
        except Exception:
            await _kill_process_tree(proc, job)
            raise
        finally:
            await _cancel_tasks(capture_tasks)
            await asyncio.gather(capture_task, return_exceptions=True)
            if job is not None:
                job.close()
        result = ProcessResult(
            command, int(proc.returncode or 0), _decode(stdout_raw), _decode(stderr_raw)
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or "程序没有提供错误详情。"
            detail = "\n".join(detail.splitlines()[-5:])[-1200:]
            raise ProcessExitError(label=label, returncode=result.returncode, diagnostic=detail)
        return result

    stdout_lines: deque[str] = deque(maxlen=400)
    stderr_lines: deque[str] = deque(maxlen=400)
    limit_event = asyncio.Event()
    actual_size = [0]
    callback_tasks: set[asyncio.Future] = set()

    def callback_done(task):
        callback_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def emit_line(raw_line: bytes, stream_name: str, target: deque[str]) -> None:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
        target.append(line)
        if on_output_line is not None:
            try:
                returned = on_output_line(line, stream_name)
                if inspect.isawaitable(returned):
                    # Do not block pipe draining on chat sending or another slow callback.
                    if len(callback_tasks) >= 16:
                        if inspect.iscoroutine(returned):
                            returned.close()
                        return
                    task = asyncio.ensure_future(returned)
                    callback_tasks.add(task)
                    task.add_done_callback(callback_done)
            except Exception:
                # Progress display must never destabilize the subprocess itself.
                pass

    async def read_stream(
        stream: asyncio.StreamReader, stream_name: str, target: deque[str]
    ) -> None:
        pending = b""
        truncated = False
        while chunk := await stream.read(8192):
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if not truncated:
                    room = 16384 - len(pending)
                    pending += part[:room]
                    truncated = len(part) > room
                if index < len(parts) - 1:
                    emit_line(
                        pending + (b" [truncated]" if truncated else b""), stream_name, target
                    )
                    pending = b""
                    truncated = False
        if pending or truncated:
            emit_line(pending + (b" [truncated]" if truncated else b""), stream_name, target)

    async def watch_size() -> None:
        if monitor_directory is None or max_directory_size_bytes is None:
            return
        while proc.returncode is None and not limit_event.is_set():
            current = await asyncio.to_thread(_directory_size, monitor_directory, monitor_pattern)
            if current > max_directory_size_bytes:
                actual_size[0] = current
                limit_event.set()
                return
            await asyncio.sleep(0.25)

    reader_tasks = [
        asyncio.create_task(read_stream(proc.stdout, "stdout", stdout_lines)),
        asyncio.create_task(read_stream(proc.stderr, "stderr", stderr_lines)),
    ]
    waiter = asyncio.create_task(proc.wait())
    watcher = asyncio.create_task(watch_size())
    limit_waiter = asyncio.create_task(limit_event.wait())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        done, _ = await asyncio.wait(
            {waiter, limit_waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            await _kill_process_tree(proc, job)
            raise ProcessTimeout(f"{label}超过 {timeout:g} 秒仍未完成，已终止进程。")
        if limit_waiter in done and limit_event.is_set():
            await _kill_process_tree(proc, job)
            maximum = int(max_directory_size_bytes or 0)
            raise ProcessOutputLimit(
                f"{label}下载期间文件总大小超过限制（实际约 {actual_size[0] / 1024 / 1024:.1f} MiB，限制 {maximum / 1024 / 1024:.1f} MiB），已终止下载。",
                actual_bytes=actual_size[0],
                max_bytes=maximum,
            )
        await asyncio.wait_for(
            asyncio.gather(*reader_tasks),
            timeout=max(0.01, deadline - asyncio.get_running_loop().time()),
        )
    except TimeoutError as exc:
        await _kill_process_tree(proc, job)
        raise ProcessTimeout(f"{label}超过 {timeout:g} 秒仍未完成，已终止进程。") from exc
    except asyncio.CancelledError:
        await _kill_process_tree(proc, job)
        raise
    except Exception:
        await _kill_process_tree(proc, job)
        raise
    finally:
        if job is not None:
            job.close()
        await _cancel_tasks([*reader_tasks, waiter, watcher, limit_waiter, *callback_tasks])

    result = ProcessResult(
        command,
        int(proc.returncode or 0),
        "\n".join(stdout_lines).strip(),
        "\n".join(stderr_lines).strip(),
    )
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "程序没有提供错误详情。"
        detail = "\n".join(detail.splitlines()[-8:])[-1800:]
        raise ProcessExitError(label=label, returncode=result.returncode, diagnostic=detail)
    return result
