from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from app.max_client import MaxMessage

log = logging.getLogger(__name__)

_STOP = object()


class MessageDispatchQueue:
    """Bounded async dispatcher with per-chat ordering."""

    def __init__(
        self,
        handler: Callable[[MaxMessage], Awaitable[None]],
        *,
        maxsize: int = 128,
        worker_count: int = 4,
    ) -> None:
        self._handler = handler
        self._queue: asyncio.Queue[MaxMessage | object] = asyncio.Queue(maxsize=maxsize)
        self._worker_count = max(1, worker_count)
        self._workers: list[asyncio.Task[None]] = []
        self._chat_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        if self._workers:
            return
        for index in range(self._worker_count):
            task = asyncio.create_task(self._worker(index))
            self._workers.append(task)

    async def stop(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self._queue.put(_STOP)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def join(self) -> None:
        await self._queue.join()

    async def submit(self, msg: MaxMessage) -> None:
        if self._queue.full():
            log.warning(
                "Message dispatch queue is full (%d), applying backpressure",
                self._queue.maxsize,
            )
        await self._queue.put(msg)

    async def _worker(self, worker_index: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return

                chat_key = str(item.chat_id)
                lock = self._chat_locks.setdefault(chat_key, asyncio.Lock())
                async with lock:
                    await self._handler(item)
            except Exception:
                log.exception("Dispatch worker %d failed while handling message", worker_index)
            finally:
                self._queue.task_done()
