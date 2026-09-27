"""Read-only Shadow context and status projections."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .contracts import ShadowContextFrame, ShadowContextSnapshot, ShadowContextUpdate
from .prompts import FORCED_STATUS_OBSERVER_PROMPT, STATUS_OBSERVER_PROMPT


class ShadowContextSource(Protocol):
    @property
    def shadow_input_revision(self) -> int: ...

    @property
    def shadow_context_epoch(self) -> int: ...

    def shadow_context_snapshot(self) -> ShadowContextSnapshot: ...

    def shadow_context_update(self, since_revision: int, *, epoch: int) -> ShadowContextUpdate | ShadowContextSnapshot: ...


def _frame_payload(frame: ShadowContextFrame) -> dict[str, Any]:
    return {
        "revision": frame.revision, "origin": frame.origin,
        "kind": frame.kind, "content": frame.content,
    }


def _text_from_output(output: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                    value = block.get("text")
                    if isinstance(value, str):
                        parts.append(value)
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


class ShadowStatusObserver:
    """Stateless, read-only summarizer over an immutable committed prefix."""

    def __init__(self, *, max_output_tokens: int = 512, timeout_ms: int | None = None) -> None:
        self.max_output_tokens = max_output_tokens
        self.timeout_ms = timeout_ms

    async def summarize(
        self, trace: Sequence[dict[str, Any]], complete: Callable[..., Any], *, forced: bool = False,
    ) -> str:
        response = await complete(
            input_items=[
                *trace,
                {"role": "system", "content": (
                    FORCED_STATUS_OBSERVER_PROMPT if forced else STATUS_OBSERVER_PROMPT
                )},
            ],
            tools=[], reasoning_effort="none", max_output_tokens=self.max_output_tokens,
            timeout_ms=self.timeout_ms, retry=False,
        )
        return _text_from_output(response.output).strip()
