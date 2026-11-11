"""In-process publish/subscribe used to stream live activity to the browser (Server-Sent Events)."""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait({"kind": kind, "data": data})
            except asyncio.QueueFull:
                # A stalled browser tab must never slow down a payment decision.
                self._subscribers.discard(queue)
