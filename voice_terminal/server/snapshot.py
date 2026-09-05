from __future__ import annotations

from typing import Any

from ..core.events import VoiceEvent, utc_now_iso
from ..core.state import RuntimeState


class ProtocolSequencer:
    def __init__(self) -> None:
        self._seq = 0

    def envelope(self, event: VoiceEvent) -> dict[str, Any]:
        self._seq += 1
        return {
            "type": event.type,
            "seq": self._seq,
            "timestamp": event.timestamp or utc_now_iso(),
            "turn_id": event.turn_id,
            "payload": event.payload,
        }


def snapshot_event(state: RuntimeState) -> VoiceEvent:
    return VoiceEvent(type="runtime.snapshot", turn_id=None, payload=state.snapshot())
