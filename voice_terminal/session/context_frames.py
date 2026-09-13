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
    KIND_KEY = "context_kind"

    @classmethod
    def make(
        cls, lane: ContextLane, role: str, content: str, *, context_kind: str | None = None,
    ) -> dict[str, str]:
        if not isinstance(lane, ContextLane):
            raise ValueError("model context frame requires a ContextLane")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("model context frame role must be system, user, or assistant")
        if not isinstance(content, str):
            raise ValueError("model context frame content must be text")
        if context_kind is not None and (not isinstance(context_kind, str) or not context_kind):
            raise ValueError("model context frame kind must be a non-empty string")
        frame = {"role": role, "content": content, cls.KEY: lane.value}
        if context_kind is not None:
            frame[cls.KIND_KEY] = context_kind
        return frame

    @classmethod
    def lane_of(cls, value: dict[str, Any]) -> ContextLane:
        try:
            return ContextLane(value.get(cls.KEY))
        except (TypeError, ValueError) as exc:
            raise ValueError("model context frame is missing a valid context_lane") from exc

    @classmethod
    def kind_of(cls, value: dict[str, Any]) -> str | None:
        kind = value.get(cls.KIND_KEY)
        if kind is None:
            return None
        if not isinstance(kind, str) or not kind:
            raise ValueError("model context frame has an invalid context_kind")
        return kind
