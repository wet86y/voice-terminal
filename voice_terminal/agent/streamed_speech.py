"""speak（新播报）的唯一受理入口：空闲创建，同一请求持续写入。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamedSpeechAdmissionState:
    """请求快照与受理结果；忙碌拒绝在整个请求中保持有效。"""

    identity: tuple  # identity（会话和请求标识）。
    source_turn_id: str | None  # source_turn_id（来源用户轮次）。
    snapshot: Any  # snapshot（请求生成时的输出快照）。
    context_locked_chars: int  # context_locked_chars（请求中锁定文本长度）。
    system_check_id: str | None  # system_check_id（系统检查标识）。
    same_turn_transcript_reassessment: bool  # 同轮转写更新评估。
    dispatch_context: dict | None  # dispatch_context（调度上下文）。
    speech_handle: Any = None  # speech_handle（本请求受理的播报事务）。
    first_speech_text_at: float | None = None  # 首次写入时间。
    prefix_baseline: dict | None = None  # prefix_baseline（已播文本去重基线）。
    speech_fed_chars: int = 0  # speech_fed_chars（实际写入字符数）。
    source_chars_fed: int = 0  # source_chars_fed（已处理的模型原文字符数）。
    prefix_chars_stripped: int = 0  # prefix_chars_stripped（已播前缀去除长度）。
    rejected: bool = False  # rejected（本请求已拒绝，播放结束后也不重新受理）。


def output_busy(output, snapshot) -> bool:
    """output_busy（输出忙碌）：覆盖排队、播放及播放结束后的事务结算。"""
    return bool(getattr(output, "busy", False) or (
        isinstance(snapshot, dict) and (
            snapshot.get("_output_busy", False)
            or snapshot.get("queue_status", "empty") != "empty"
        )
    ))


async def queue_streamed_speech(runtime, state: StreamedSpeechAdmissionState,
                                decision, full_text, *, final=False) -> bool:
    """统一处理完整响应和流式响应；此入口只调用 begin（创建），并独占文本写入。"""
    if state.rejected:
        return True
    if state.speech_handle is None:
        if output_busy(runtime.output, state.snapshot):
            state.rejected = True
            runtime.minor_protocol_feedback.append({
                "field": "speak.state",
                "applied": "ignored_output_busy",
                "hint": "输出仍忙碌：speak（新播报）不能追加或替换。确有实质不符或必要补充且尾巴可改时"
                        "才用 revision（尾巴修订）；取消重讲必须满足末尾严格纠错条件，否则 wait（等待）。",
            })
            await runtime.log("SPEAK_BUSY_IGNORED", identity=state.identity,
                              turn_id=state.source_turn_id,
                              queue_status=(state.snapshot.get("queue_status", "empty")
                                            if isinstance(state.snapshot, dict) else "empty"),
                              system_check_id=state.system_check_id)
            return True
        if getattr(runtime, "should_defer_speech", lambda: False)():
            return True
        if not full_text.strip():
            return True
        # 空闲时保留已播文本的去重保护；它不赋予活动事务追加权限。
        if state.prefix_baseline is None:
            state.prefix_baseline = runtime._completed_speech_guard(
                state.identity, state.source_turn_id,
                allow_same_turn=state.same_turn_transcript_reassessment,
            )
            if (state.prefix_baseline is None and isinstance(state.snapshot, dict)
                    and state.snapshot.get("delivered_turn_id") == state.source_turn_id
                    and state.snapshot.get("delivered_resumable", True)):
                delivered = str(state.snapshot.get("delivered_text") or "")
                if delivered:
                    state.prefix_baseline = {"text": delivered, "speech_id": None,
                                             "source": "same_turn_delivered_frontier"}
        guard = state.prefix_baseline
        baseline = guard["text"] if guard else ""
        if baseline and baseline.startswith(full_text) and not final:
            return True
        if (baseline and full_text == baseline and final
                and guard["source"] not in {"completed_same_turn_guard", "same_turn_delivered_frontier"}):
            # 空闲后的明确复述仍可新建播报。
            state.source_chars_fed = 0
        elif baseline and full_text.startswith(baseline):
            state.source_chars_fed = len(baseline)
        if not full_text[state.source_chars_fed:].strip():
            if final:
                state.rejected = True
                runtime._note_identical_speak(guard["source"] if guard else "empty_text")
                await runtime.log("SPEECH_IDENTICAL_IGNORED", identity=state.identity,
                                  turn_id=state.source_turn_id,
                                  baseline_source=guard["source"] if guard else "empty_text",
                                  reason="completed_same_turn_identical")
            return True
        handle = await runtime.output.begin(state.identity, state.source_turn_id,
                                            emotion=decision.emotion)
        # begin（创建）在异步准备后再次确认空闲，防止并发请求同时通过。
        if handle is None:
            state.rejected = True
            await runtime.log("SPEAK_BUSY_IGNORED", identity=state.identity,
                              turn_id=state.source_turn_id, reason="admission_boundary_changed")
            return True
        state.speech_handle = handle
        state.prefix_chars_stripped = state.source_chars_fed
        state.first_speech_text_at = time.monotonic()
        runtime._track_system_check_speech(handle, state.dispatch_context)
        workspace = getattr(handle, "workspace", None)
        if workspace is not None and isinstance(getattr(workspace, "timings", None), dict):
            workspace.timings["speak_queued"] = state.first_speech_text_at
            workspace.timings["first_speech_text"] = state.first_speech_text_at
            workspace.timings["speech_started_before_envelope_commit"] = not final
        await runtime.log("SPEAK_ACCEPTED", identity=state.identity,
                          turn_id=state.source_turn_id, speech_id=handle.speech_id,
                          replaced=False, partial=not final)
        if state.source_chars_fed:
            await runtime.log("SPEECH_COMPLETED_SAME_TURN_PREFIX_STRIPPED"
                              if guard["source"] == "completed_same_turn_guard"
                              else "CROSS_TURN_SPEECH_PREFIX_STRIPPED",
                              identity=state.identity, turn_id=state.source_turn_id,
                              baseline_source=guard["source"], stripped=True,
                              reason="completed_same_turn_extension"
                              if guard["source"] == "completed_same_turn_guard"
                              else "completed_previous_turn_prefix",
                              baseline_chars=state.source_chars_fed,
                              suffix_chars=len(full_text) - state.source_chars_fed)
    if getattr(state.speech_handle, "reason", None):
        return True
    value = full_text[state.source_chars_fed:]
    if value:
        await state.speech_handle.feed(value)
        state.source_chars_fed = len(full_text)
        state.speech_fed_chars += len(value)
    return True
