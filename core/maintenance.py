"""Coordinate binary users with exclusive maintenance and cancellation-safe file I/O."""

from __future__ import annotations

import asyncio
import functools
from contextlib import asynccontextmanager

from .errors import UserFacingError


class MaintenanceUnavailable(UserFacingError):
    pass


async def run_blocking(function, /, *args, **kwargs):
    """Finish started filesystem work before cancellation permits rollback or cleanup."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                task.result()
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _finish_cleanup(awaitable):
    """Finish async resource cleanup before propagating repeated cancellation."""
    cleanup = asyncio.create_task(awaitable)
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


class MaintenanceGate:
    def __init__(self):
        self._condition = asyncio.Condition()
        self._holders: dict[asyncio.Task, int] = {}
        self.maintaining = False
        self.closed = False
        self.update_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def use(self, *, timeout: float = 120):
        task = asyncio.current_task()
        async with self._condition:
            if self.closed:
                raise MaintenanceUnavailable("插件正在卸载，不能启动新任务。")
            if task not in self._holders:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: self.closed or not self.maintaining),
                        timeout=timeout,
                    )
                except TimeoutError:
                    raise MaintenanceUnavailable("依赖正在维护，等待超时；请稍后重试。") from None
                if self.closed:
                    raise MaintenanceUnavailable("插件正在卸载，不能启动新任务。")
            self._holders[task] = self._holders.get(task, 0) + 1
        try:
            yield
        finally:

            async def release_holder():
                async with self._condition:
                    count = self._holders.get(task, 1) - 1
                    if count:
                        self._holders[task] = count
                    else:
                        self._holders.pop(task, None)
                    self._condition.notify_all()

            await _finish_cleanup(release_holder())

    @asynccontextmanager
    async def maintenance(self, *, timeout: float = 120):
        task = asyncio.current_task()
        async with self._condition:
            if self.closed or self.maintaining or task in self._holders:
                raise MaintenanceUnavailable("插件正在卸载、使用依赖或已有依赖更新任务。")
            self.maintaining = True
            self.update_tasks.add(task)
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: not self._holders or self.closed),
                    timeout=timeout,
                )
                if self.closed:
                    raise MaintenanceUnavailable("插件正在卸载，已停止依赖更新。")
            except BaseException:
                self.maintaining = False
                self.update_tasks.discard(task)
                self._condition.notify_all()
                raise
        try:
            yield
        finally:

            async def release_maintenance():
                async with self._condition:
                    self.maintaining = False
                    self.update_tasks.discard(task)
                    self._condition.notify_all()

            await _finish_cleanup(release_maintenance())

    async def close(self) -> None:
        async with self._condition:
            self.closed = True
            self._condition.notify_all()

    def task_snapshot(self) -> set[asyncio.Task]:
        return set(self._holders) | self.update_tasks


def uses_dependencies(method):
    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.dependency_gate.use(timeout=self.settings.queue_wait_timeout_seconds):
            return await method(self, *args, **kwargs)

    return wrapped
