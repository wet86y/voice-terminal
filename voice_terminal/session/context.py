"""Semantic, model-facing projection of the session ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from .ledger import SessionLedger
from .context_frames import ContextFrame, ContextLane


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
    # Final and refined transcripts are revisions of this same user turn.
    # The dictionaries intentionally contain semantic transcript evidence
    # only; runtime correlation IDs remain local.
    transcript_updates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # Internal correlation only.  It is deliberately never rendered into a
    # model-facing message, but lets the projector avoid duplicating a turn
    # that the controller has already committed to the ledger.
    turn_id: str = ""


class LLMContextProjector:
    """Expose semantic facts only; runtime IDs and playback state never cross."""

    @staticmethod
    def current_session_time() -> str:
        """Render wall-clock time as a conversational fact, never an epoch value.

        The Agent needs a human-interpretable time for requests such as
        "今天几点" or "明天", whereas request timing, scheduler timestamps and
        provider latency are operational data.  Keep those two concerns apart.
        ``astimezone`` deliberately uses the host's configured local zone: the
        voice terminal and its user therefore share the same calendar day.
        """

        now = datetime.now().astimezone()
        weekdays = "一二三四五六日"
        offset = now.strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "本地时区"
        zone = now.tzname() or "本地时区"
        return (
            f"当前会话时间：{now:%Y年%m月%d日 %H:%M:%S}，"
            f"星期{weekdays[now.weekday()]}，{zone}（{offset}）。"
        )

    @staticmethod
    def runtime_tail(*, current: CurrentInput, speak_state: dict[str, Any], calendar_epoch: str | None = None) -> str:
        """Create the only volatile model context block for one request."""

        now = datetime.now().astimezone()
        payload: dict[str, Any] = {
            "runtime": {
                "clock": {"local_time": now.strftime("%H:%M:%S"), "precision": "second"},
                "speak": speak_state,
            }
        }
        if calendar_epoch:
            payload["runtime"]["calendar_epoch"] = calendar_epoch
        if current.finalized:
            payload["runtime"]["input_status"] = "finalized"
        elif current.user_speaking is True:
            payload["runtime"]["input_status"] = "speaking"
        available = [
            {
                key: update[key]
                for key in ("kind", "source", "text", "revision", "streamed_text")
                if key in update
            }
            for update in current.transcript_updates
            if isinstance(update, dict) and str(update.get("text") or "").strip()
        ]
        triggered_by: list[str] = []
        if "USER_INPUT_FINALIZED" in current.recent_events:
            triggered_by.append("asr_final")
        if "USER_INPUT_REFINED" in current.recent_events:
            triggered_by.append("refinement")
        if available:
            payload["runtime"]["transcript_updates"] = {
                "same_user_turn": True,
                "available": available,
                "triggered_by": triggered_by,
            }
        return "当前轮运行状态：" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def project(self, ledger: SessionLedger, current: CurrentInput) -> list[dict[str, Any]]:
        primary = (current.primary_transcript or current.text or "").strip()
        messages = ledger.context_messages("")
        committed = bool(current.turn_id) and ledger.has_input(current.turn_id)
        # Finalization commits the durable user entry before the asynchronous
        # Agent runner renders its context.  Do not append the same turn a
        # second time here.  Identity is required: text de-duplication would
        # incorrectly erase a user who repeats an earlier utterance.
        if primary and not committed:
            # An uncommitted ASR candidate is current-request material. Once
            # finalized it enters the ledger and becomes an append-only frame.
            updates = list(current.transcript_updates)
            if updates:
                transcript = {
                    str(update.get("kind")): str(update.get("text") or "").strip()
                    for update in updates
                    if isinstance(update, dict)
                    and update.get("kind") in {"asr_final", "refinement"}
                    and str(update.get("text") or "").strip()
                }
                content = "用户语音最终文本：" + json.dumps(
                    transcript, ensure_ascii=False, separators=(",", ":"),
                )
            else:
                content = primary
            messages.append(ContextFrame.make(ContextLane.REQUEST_TAIL, "user", content))
        return messages
