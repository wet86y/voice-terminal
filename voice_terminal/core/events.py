from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class VoiceEvent:
    """An internal event.  The WebSocket layer converts these into protocol envelopes."""

    type: str
    turn_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)


def event(event_type: str, turn_id: str | None = None, **payload: Any) -> VoiceEvent:
    return VoiceEvent(type=event_type, turn_id=turn_id, payload=payload)
