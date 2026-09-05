from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .events import VoiceEvent


class EventBus:
    """Small in-process fan-out bus used by the controller and WebSocket clients."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[VoiceEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, value: VoiceEvent) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                # A stalled monitoring client must never stall microphone input.
                async with self._lock:
                    self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(value)

    async def subscribe(self) -> AsyncIterator[VoiceEvent]:
        queue: asyncio.Queue[VoiceEvent] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                value = await queue.get()
                if value is None:
                    raise RuntimeError("monitor subscriber fell behind; reconnect for a snapshot")
                yield value
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
