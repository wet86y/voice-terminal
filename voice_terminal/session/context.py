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
SPEECH_QUEUE_TASK_KIND = "speech_queue_task"  # 本次语音队列决策任务。
OUTPUT_CONSTRAINT_KIND = "output_constraint"
REQUEST_SYSTEM_CONTEXT_KINDS = frozenset({
    "minor_protocol_feedback",
    "immediate_recovery",
    "deferred_recovery",
    "reasoning_mode",  # reasoning_mode（本次推理模式提示）：与冻结的接口推理强度一致。
    "pending_tools",
    "final_silence",
    "kws_wake",  # kws_wake（唤醒确认）：保留本次场景类型。
    "secondary_mailbox",  # 二等信箱专用评估提示。
    "secondary_update",  # 本次请求携带的最新进度来源。
    "continuity_document",  # 会话衔接背景，不能覆盖最新用户表达。
    "scheduled_wake",  # 用户先前设置的定时唤醒事件。
    "system_context",
})
SPEECH_QUEUE_TASK = (
    "【本次任务】\n"
    "依据实际用户消息、HISTORY（完整历史上下文）、本次调度信息和当前快照，完成一次语音队列决策。"
    "只使用实际提供的状态与事件，不推断缺失事件；区分已经传达的内容和当前计划。\n"
    "没有活动队列时，按首次回答规则回应尚未回答的用户输入或仍必要的新信息；"
    "已有活动队列且计划已覆盖本次输入时，使用 wait（保持队列），让原播报继续。\n"
    "只有固定规则允许且确有实质理由时才修改队列。revision（尾巴修订）是在锁定边界后续写，"
    "speak.text（播报文本）只提交完整的新 editable_text（可改文本），直接接在 locked_text（锁定文本）后，"
    "保留旧尾巴所有仍有效的要点，不重新回答或重述锁定部分。"
    "仅当确认已听内容严重错误且局部说明不足以纠正时，才 cancel（取消并替换）。\n"
    "用户主动追问时依据已知事实回答，没有确认更新就如实说明。actions（后台动作列表）按路由独立判断。"
    "最终只输出 voice_decision_v06（语音决策结构）要求的 speak（语音决策）与 actions（动作列表）JSON（结构化输出）。"
)
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
    """Separate request facts, then place the native snapshot beside the task."""

    prefix: list[dict[str, str]] = []
    current_users: list[dict[str, Any]] = []
    system_frames: list[dict[str, str]] = []
    snapshot: dict[str, Any] | None = None
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
            if system_frames or snapshot is not None:
                raise RuntimeError("current user input must precede request system context")
            current_users.append(frame)
            continue
        if role != "system":
            raise RuntimeError("request tail supports only user and system frames")
        if kind in {VOICE_REQUEST_CONTEXT_TYPE, SPEECH_QUEUE_TASK_KIND, OUTPUT_CONSTRAINT_KIND}:
            raise RuntimeError("request tail was already consolidated")
        if kind == "runtime_state":
            if snapshot is not None:
                raise RuntimeError("request tail may contain only one runtime snapshot")
            # runtime_state（运行状态）是完整 JSON（结构化数据），不再作为字符串嵌套。
            try:
                snapshot = json.loads(content.removeprefix("当前轮运行状态："))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("runtime snapshot must be valid JSON") from exc
            if not isinstance(snapshot, dict):
                raise RuntimeError("runtime snapshot must be an object")
        else:
            system_frames.append(ContextFrame.make(
                ContextLane.REQUEST_TAIL, "system", content,
                context_kind=kind if kind in REQUEST_SYSTEM_CONTEXT_KINDS else "system_context",
            ))
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
    result.extend(system_frames)
    result.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL,
        "system",
        _canonical_json({
            "type": VOICE_REQUEST_CONTEXT_TYPE,
            "version": 2,  # 第2版输入封装；输出协议仍为 voice_decision_v06（语音决策结构）。
            **(snapshot or {}),
        }),
        context_kind=VOICE_REQUEST_CONTEXT_TYPE,
    ))
    result.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL, "system", SPEECH_QUEUE_TASK,
        context_kind=SPEECH_QUEUE_TASK_KIND,
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
        tool_availability: dict[str, str] | None = None,
    ) -> str:
        """Create the only volatile model context block for one request."""

        now = datetime.now().astimezone()
        speech = dict(speak_state)
        parts = speech.pop("text_parts", {})
        if isinstance(parts, dict):
            # 本地仍保留四段权威边界，模型直接读取完整锁定文本和可改文本。
            speech["locked_text"] = "".join(
                str(parts.get(key) or "")
                for key in ("committed_prefix", "current_clause", "prefetched_clause")
            )
            speech["editable_text"] = str(parts.get("revisable_tail") or "")
        payload: dict[str, Any] = {
            "runtime": {
                "clock": {"local_time": now.strftime("%H:%M:%S"), "precision": "second"},
            },
            "speech": speech,
        }
        if tool_availability is not None:
            payload["tool_availability"] = dict(tool_availability)
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
            progress = shadow.get("progress")
            if isinstance(progress, list):
                values = [
                    {"text": str(item["text"])[:512], "source": item["source"]}
                    for item in progress if isinstance(item, dict)
                    and str(item.get("text") or "").strip()
                    and item.get("source") in {"agent", "observer"}
                ]
                if values:
                    payload["shadow"]["progress"] = values  # 进度列表，仅在本次请求尾部提供。
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
