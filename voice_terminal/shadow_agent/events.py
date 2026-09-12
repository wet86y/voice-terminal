"""Coalescing event mailbox for future heartbeat integration."""
from __future__ import annotations

from collections import OrderedDict

from .contracts import ShadowTaskEvent


_TERMINAL = {"completed", "cancelled", "timed_out", "failed"}


class ShadowTaskEventBuffer:
    """Keep one pending event per task while preserving first enqueue order."""

    def __init__(self) -> None:
        self._pending: OrderedDict[int, ShadowTaskEvent] = OrderedDict()

    def publish(self, event: ShadowTaskEvent) -> None:
        current = self._pending.get(event.id)
        if current is not None and current.status in _TERMINAL and event.status == "running":
            return
        # Assignment to an existing OrderedDict key deliberately retains its
        # original position, giving different task IDs stable event ordering.
        self._pending[event.id] = event

    def drain(self) -> list[ShadowTaskEvent]:
        result = list(self._pending.values())
        self._pending.clear()
        return result
