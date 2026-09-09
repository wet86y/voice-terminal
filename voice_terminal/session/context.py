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
    # ``text`` remains the backwards-compatible primary transcript field.
    # These explicit fields let the Agent distinguish the fast Paraformer
    # commit from a later SenseVoice supplement without exposing runtime IDs.
    primary_transcript: str = ""
    refined_transcript: str = ""
    refinement_available: bool = False
    # Internal correlation only.  It is deliberately never rendered into a
    # model-facing message, but lets the projector avoid duplicating a turn
    # that the controller has already committed to the ledger.
    turn_id: str = ""


class LLMContextProjector:
    """Expose semantic facts only; runtime IDs and playback state never cross."""

    def project(self, ledger: SessionLedger, current: CurrentInput) -> list[dict[str, Any]]:
        primary = (current.primary_transcript or current.text or "").strip()
        refined = (current.refined_transcript or "").strip()
        messages = ledger.context_messages("")
        committed = bool(current.turn_id) and ledger.has_input(current.turn_id)
        # Finalization commits the durable user entry before the asynchronous
        # Agent runner renders its context.  Do not append the same turn a
        # second time here.  Identity is required: text de-duplication would
        # incorrectly erase a user who repeats an earlier utterance.
        if primary and not committed:
            messages.append({"role": "user", "content": primary})
        facts: list[str] = []
        if current.finalized:
            facts.append("当前用户输入已经最终确认。")
        elif current.user_speaking is True:
            facts.append("用户当前仍在讲话；可在 V0.6 信封中使用 speak(text) 建立文本候选，但系统只会在本地确认用户结束讲话后才启动 TTS。")
        durable_refinement = ledger.input_refinement(current.turn_id) if committed else ""
        if current.refinement_available and refined and durable_refinement != refined:
            facts.append("SenseVoice 已返回补充精修文本：" + refined)
            facts.append("精修文本是补充证据，不是开始首轮回答的前置条件；如已播放内容确实与精修语义冲突，可用 speak(text) 请求受控替换。")
        if current.recent_events:
            facts.append("最近语义事件：" + "、".join(current.recent_events))
        if facts:
            messages.append({"role": "system", "content": "\n".join(facts)})
        return messages
