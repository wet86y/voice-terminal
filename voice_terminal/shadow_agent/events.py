"""Coalescing mailbox between the Shadow execution plane and heartbeat."""
from __future__ import annotations

from collections import OrderedDict

from .contracts import ShadowEvent


class ShadowEventBuffer:
    """Coalesce visible state while preserving every semantic event."""

    def __init__(self) -> None:
        self._state: OrderedDict[str, ShadowEvent] = OrderedDict()
        self._semantic: list[ShadowEvent] = []

    def publish(self, event: ShadowEvent) -> None:
        if event.kind in {"status", "progress"}:
            self._state[event.kind] = event
        else:
            self._semantic.append(event)

    def drain(self) -> list[ShadowEvent]:
        result = [*self._state.values(), *self._semantic]
        self._state.clear()
        self._semantic.clear()
        return result
