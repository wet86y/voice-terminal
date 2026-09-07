"""Semantic, model-facing projection of the session ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ledger import SessionLedger


@dataclass(frozen=True)
class CurrentInput:
    text: str = ""
    finalized: bool = False
    user_speaking: bool | None = None
    recent_events: tuple[str, ...] = field(default_factory=tuple)


class LLMContextProjector:
    """Expose semantic facts only; runtime IDs and playback state never cross."""

    def project(self, ledger: SessionLedger, current: CurrentInput) -> list[dict[str, Any]]:
        messages = ledger.context_messages("")
        if current.text.strip():
            messages.append({"role": "user", "content": current.text})
        facts: list[str] = []
        if current.finalized:
            facts.append("当前用户输入已经最终确认。")
        elif current.user_speaking is True:
            facts.append("用户当前仍在讲话；不要调用 speak。")
        if current.recent_events:
            facts.append("最近语义事件：" + "、".join(current.recent_events))
        if facts:
            messages.append({"role": "system", "content": "\n".join(facts)})
        return messages
