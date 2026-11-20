from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from .. import agent_debug
from .events import VoiceEvent


class EventBus:
    """Small in-process fan-out bus used by the controller and WebSocket clients."""

    def __init__(self, observe: Callable[[VoiceEvent], None] | None = None) -> None:
        self._observe = observe
        self._subscribers: set[asyncio.Queue[VoiceEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, value: VoiceEvent) -> None:
        if agent_debug.ENABLED and not value.type.startswith(("audio.level", "asr.partial")):
            await agent_debug.point(value.type, "event", {"turn_id": value.turn_id, "payload": value.payload})
        if self._observe is not None:
            self._observe(value)
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
