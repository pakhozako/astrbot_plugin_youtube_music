"""Bounded FIFO admission for complete search and playback jobs."""

import asyncio
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager

from .errors import UserFacingError

MAX_BATCH_SIZE = 20


def pending_queue_summary(rows):
    """Summarize pending rows, not processing delays or a playback ETA."""
    count = duration_seconds = unknown_duration_count = 0
    for row in rows:
        count += 1
        song = row.get("song") if isinstance(row, dict) else None
        duration = song.get("duration") if isinstance(song, dict) else None
        if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
            duration_seconds += duration
        else:
            unknown_duration_count += 1
    return {
        "count": count,
        "duration_seconds": duration_seconds,
        "unknown_duration_count": unknown_duration_count,
    }


class QueueFull(UserFacingError):
    pass


class QueueWaitTimeout(UserFacingError):
    pass


class JobLimiter:
    def __init__(self, max_concurrent: int, max_queued: int):
        self.max_concurrent = max_concurrent
        self.max_queued = max_queued
        self._active = 0
        self._waiters: list[str] = []
        self._condition = asyncio.Condition()

    @property
    def active(self):
        return self._active

    @asynccontextmanager
    async def slot(self, *, wait_timeout: float, on_state: Callable | None = None):
        async with self._condition:
            ticket = None
            if self._active >= self.max_concurrent or self._waiters:
                if len(self._waiters) >= self.max_queued:
                    raise QueueFull("处理任务已满，请稍后重试。")
                ticket = uuid.uuid4().hex
                self._waiters.append(ticket)
                try:
                    if on_state:
                        on_state("等待处理")
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: (
                                self._active < self.max_concurrent and self._waiters[0] == ticket
                            )
                        ),
                        wait_timeout,
                    )
                    self._waiters.pop(0)
                except TimeoutError:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                    raise QueueWaitTimeout("等待处理超时，请稍后重试。") from None
                except BaseException:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                    raise
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()
