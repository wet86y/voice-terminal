"""Cache-placement contract for model-facing context frames."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ContextLane(StrEnum):
    """Explicit cache placement for every model-facing frame."""

    STATIC_INSTRUCTIONS = "static_instructions"
    HISTORY = "history"
    REQUEST_TAIL = "request_tail"


class ContextFrame:
    """Factory and validator for the internal provider-neutral message shape."""

    KEY = "context_lane"

    @classmethod
    def make(cls, lane: ContextLane, role: str, content: str) -> dict[str, str]:
        if not isinstance(lane, ContextLane):
            raise ValueError("model context frame requires a ContextLane")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("model context frame role must be system, user, or assistant")
        if not isinstance(content, str):
            raise ValueError("model context frame content must be text")
        return {"role": role, "content": content, cls.KEY: lane.value}

    @classmethod
    def lane_of(cls, value: dict[str, Any]) -> ContextLane:
        try:
            return ContextLane(value.get(cls.KEY))
        except (TypeError, ValueError) as exc:
            raise ValueError("model context frame is missing a valid context_lane") from exc
