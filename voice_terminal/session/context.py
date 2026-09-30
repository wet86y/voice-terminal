"""Semantic, model-facing projection of the session ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from .ledger import SessionLedger
from .context_frames import ContextFrame, ContextLane


CURRENT_USER_INPUT_TYPE = "current_user_input"
VOICE_REQUEST_CONTEXT_TYPE = "voice_request_context"
OUTPUT_CONSTRAINT_KIND = "output_constraint"
REQUEST_SYSTEM_CONTEXT_KINDS = frozenset({
    "minor_protocol_feedback",
    "immediate_recovery",
    "deferred_recovery",
    "runtime_state",
    "pending_tools",
    "final_silence",
    "system_context",
})
FINAL_OUTPUT_CONSTRAINT = (
    "输出约束：最终 output_text 必须且只能是一个符合 voice_decision_v06 的完整 JSON 对象。"
    "首个非空白字符必须是 {，末个非空白字符必须是 }。禁止输出解释、道歉、开场白、"
    "Markdown、代码围栏或 JSON 之外的任何文字。"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def wrap_current_user_input(content: str) -> str:
    """Wrap volatile user text without changing any of its content."""

    return _canonical_json({
        "type": CURRENT_USER_INPUT_TYPE,
        "version": 1,
        "content": content,
    })


def consolidate_request_tail(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return a lossless, canonical request tail followed by the output guard."""

    prefix: list[dict[str, str]] = []
    current_users: list[dict[str, Any]] = []
    system_entries: list[dict[str, Any]] = []
    tail_started = False
    for raw in messages:
        frame = dict(raw)
        lane = ContextFrame.lane_of(frame)
        if lane != ContextLane.REQUEST_TAIL:
            if tail_started:
                raise RuntimeError("non-tail frame cannot follow request tail")
            prefix.append(frame)
            continue
        tail_started = True
        role = str(frame.get("role") or "")
        kind = ContextFrame.kind_of(frame)
        content = str(frame.get("content") or "")
        if role == "user":
            if system_entries:
                raise RuntimeError("current user input must precede request system context")
            current_users.append(frame)
            continue
        if role != "system":
            raise RuntimeError("request tail supports only user and system frames")
        if kind in {VOICE_REQUEST_CONTEXT_TYPE, OUTPUT_CONSTRAINT_KIND}:
            raise RuntimeError("request tail was already consolidated")
        system_entries.append({
            "index": len(system_entries),
            "kind": kind if kind in REQUEST_SYSTEM_CONTEXT_KINDS else "system_context",
            "content": content,
        })
    if len(current_users) > 1:
        raise RuntimeError("request tail may contain at most one current user input")

    result = list(prefix)
    if current_users:
        current = current_users[0]
        result.append(ContextFrame.make(
            ContextLane.REQUEST_TAIL,
            "user",
            wrap_current_user_input(str(current.get("content") or "")),
            context_kind=CURRENT_USER_INPUT_TYPE,
        ))
    result.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL,
        "system",
        _canonical_json({
            "type": VOICE_REQUEST_CONTEXT_TYPE,
            "version": 1,
            "entries": system_entries,
        }),
        context_kind=VOICE_REQUEST_CONTEXT_TYPE,
    ))
    result.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL,
        "system",
        FINAL_OUTPUT_CONSTRAINT,
        context_kind=OUTPUT_CONSTRAINT_KIND,
    ))
    return result


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
    def _refinement_cues(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        cues = []
        for segment in value[:32]:
            if not isinstance(segment, dict):
                continue
            emotion = segment.get("emotion")
            emotion = emotion if isinstance(emotion, str) and emotion not in {"", "unknown"} else None
            raw_events = segment.get("events")
            events = (
                [item for item in raw_events[:8]
                 if isinstance(item, str) and item and item != "Speech"]
                if isinstance(raw_events, list) else []
            )
            if not emotion and not events:
                continue
            cue: dict[str, Any] = {}
            for key in ("start_ms", "end_ms"):
                boundary = segment.get(key)
                if type(boundary) is int and boundary >= 0:
                    cue[key] = boundary
            if emotion:
                cue["emotion"] = emotion[:32]
            if events:
                cue["events"] = [item[:32] for item in events]
            cues.append(cue)
        return cues

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
    def runtime_tail(
        *, current: CurrentInput, speak_state: dict[str, Any],
        calendar_epoch: str | None = None, shadow: dict[str, Any] | None = None,
    ) -> str:
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
        if shadow is not None:
            payload["shadow"] = {
                "available": bool(shadow.get("available")),
                "active": bool(shadow.get("active")),
                "user_input_required": bool(shadow.get("user_input_required")),
            }
            information = shadow.get("information")
            if isinstance(information, dict) and str(information.get("text") or "").strip():
                payload["shadow"]["information"] = {
                    "text": str(information.get("text") or "")[:2000],
                    "source": str(information.get("source") or "agent"),
                }
        if current.finalized:
            payload["runtime"]["input_status"] = "finalized"
        elif current.user_speaking is True:
            payload["runtime"]["input_status"] = "speaking"
        available = []
        for update in current.transcript_updates:
            if not isinstance(update, dict) or not str(update.get("text") or "").strip():
                continue
            item = {
                key: update[key]
                for key in ("kind", "source", "text", "revision", "streamed_text")
                if key in update
            }
            if update.get("kind") == "refinement" and "USER_INPUT_REFINED" in current.recent_events:
                cues = LLMContextProjector._refinement_cues(update.get("audio_cues"))
                if cues:
                    item["audio_cues"] = cues
            available.append(item)
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
            messages.append(ContextFrame.make(
                ContextLane.REQUEST_TAIL, "user", content,
                context_kind=CURRENT_USER_INPUT_TYPE,
            ))
        return messages
