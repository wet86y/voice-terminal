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
            self._state[self._state_key(event)] = event
        else:
            for key, pending in tuple(self._state.items()):
                if pending.kind == "progress" and pending.activation_id == event.activation_id:
                    self._state.pop(key)
            self._semantic.append(event)

    @staticmethod
    def _state_key(event: ShadowEvent) -> str:
        # information（影子信息）仍是全局单槽；progress（进度）按激活和来源合并。
        return (f"progress:{event.activation_id}:{event.source}"
                if event.kind == "progress" else event.kind)

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
                restored_state[self._state_key(event)] = event
            else:
                restored_semantic.append(event)
        for kind, event in restored_state.items():
            self._state.setdefault(kind, event)
        if restored_semantic:
            self._semantic[:0] = restored_semantic
