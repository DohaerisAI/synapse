from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine


class ExecutionLane:
    def __init__(self, name: str, *, max_size: int = 100) -> None:
        self.name = name
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_size)
        self._handler: Callable[[Any], Coroutine[Any, Any, None]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def set_handler(self, handler: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        self._handler = handler

    async def enqueue(self, item: Any) -> None:
        await self._queue.put(item)

    def enqueue_nowait(self, item: Any) -> None:
        self._queue.put_nowait(item)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            if self._handler is not None:
                try:
                    await self._handler(item)
                except Exception:
                    pass

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class CommandQueue:
    def __init__(self) -> None:
        self.main = ExecutionLane("main")
        self.cron = ExecutionLane("cron")
        self._lanes: dict[str, ExecutionLane] = {
            "main": self.main,
            "cron": self.cron,
        }

    def get_lane(self, name: str) -> ExecutionLane | None:
        return self._lanes.get(name)

    def add_lane(self, lane: ExecutionLane) -> None:
        self._lanes[lane.name] = lane

    def start_all(self) -> None:
        for lane in self._lanes.values():
            lane.start()

    async def stop_all(self) -> None:
        for lane in self._lanes.values():
            await lane.stop()
