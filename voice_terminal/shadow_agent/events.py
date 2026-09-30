"""Coalescing mailbox between the Shadow execution plane and heartbeat."""
from __future__ import annotations

from collections import OrderedDict

from .contracts import ShadowEvent


class ShadowEventBuffer:
    """Coalesce replaceable state while preserving terminal semantic events."""

    def __init__(self) -> None:
        self._state: OrderedDict[str, ShadowEvent] = OrderedDict()
        self._semantic: list[ShadowEvent] = []

    def publish(self, event: ShadowEvent) -> None:
        if event.kind in {"progress", "information"}:
            self._state[event.kind] = event
        else:
            self._semantic.append(event)

    def drain(self) -> list[ShadowEvent]:
        result = [*self._state.values(), *self._semantic]
        self._state.clear()
        self._semantic.clear()
        return result

    def restore(self, events: list[ShadowEvent]) -> None:
        """Put an undelivered batch back without overwriting newer state.

        A relay removes a batch temporarily while awaiting its consumer.  The
        consumer may fail after a newer status/progress event has already
        arrived.  Restoring the older batch must preserve that newer visible
        state, while semantic events retain their original delivery order.
        """
        restored_state: OrderedDict[str, ShadowEvent] = OrderedDict()
        restored_semantic: list[ShadowEvent] = []
        for event in events:
            if event.kind in {"progress", "information"}:
                restored_state[event.kind] = event
            else:
                restored_semantic.append(event)
        for kind, event in restored_state.items():
            self._state.setdefault(kind, event)
        if restored_semantic:
            self._semantic[:0] = restored_semantic
