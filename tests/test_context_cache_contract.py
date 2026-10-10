from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime, RecoverySignal
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.prompts import SPEECH_QUEUE_DECISION_PROMPT
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context import (
    CURRENT_USER_INPUT_TYPE, FINAL_OUTPUT_CONSTRAINT, SPEECH_QUEUE_TASK,
    SPEECH_QUEUE_TASK_KIND, VOICE_REQUEST_CONTEXT_TYPE, CurrentInput,
    LLMContextProjector, consolidate_request_tail,
)
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def _runtime_state_frame(snapshot):
    # runtime_state（运行状态）必须是带有 runtime（运行信息）与 speech（语音状态）的 JSON 对象。
    return ContextFrame.make(
        ContextLane.REQUEST_TAIL, "system",
        "当前轮运行状态：" + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        context_kind="runtime_state",
    )


def _request_packet(messages):
    # voice_request_context（语音请求上下文）现在直接承载根级原生 JSON。
    frame = next(
        item for item in messages
        if ContextFrame.kind_of(item) == VOICE_REQUEST_CONTEXT_TYPE
    )
    return json.loads(frame["content"])


def _current_input(messages):
    # current_user_input（当前用户输入）单独保留为动态用户帧。
    frame = next(
        item for item in messages
        if ContextFrame.kind_of(item) == CURRENT_USER_INPUT_TYPE
    )
    return json.loads(frame["content"])


def test_responses_projects_only_static_frames_to_instructions_and_hides_lanes():
    payload = DeepSeekClient._responses_payload(
        [
            ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "stable rules"),
            ContextFrame.make(ContextLane.HISTORY, "assistant", "heard text"),
            ContextFrame.make(
                ContextLane.REQUEST_TAIL, "system", "live state",
                context_kind="runtime_state",
            ),
        ],
        model="deepseek", schema=decision_schema([]), schema_name="decision",
        reasoning_effort="none", max_output_tokens=100,
    )
    assert payload["instructions"].startswith("stable rules")
    assert [item["role"] for item in payload["input"]] == ["assistant", "system"]
    assert [item["content"][0]["text"] for item in payload["input"]] == ["heard text", "live state"]
    assert "context_lane" not in str(payload)
    assert "context_kind" not in str(payload)


def test_responses_rejects_model_message_without_lane():
    with pytest.raises(ValueError, match="context_lane"):
        DeepSeekClient._responses_payload(
            [{"role": "user", "content": "missing lane"}],
            model="deepseek", schema=decision_schema([]), schema_name="decision",
            reasoning_effort="none", max_output_tokens=100,
        )


def test_runtime_tail_is_after_current_user_and_time_is_not_static():
    ledger = SessionLedger()
    runtime = ConversationRuntime()
    projector = LLMContextProjector()
    current = CurrentInput(text="现在精确到秒是多少", primary_transcript="现在精确到秒是多少")
    messages = [ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", runtime.calendar_baseline)]
    messages.extend(projector.project(ledger, current))
    messages.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL, "system", projector.runtime_tail(current=current, speak_state={
            "state": "speaking", "owner_scope": "current_turn",
            "continuation_mode": "rewrite_tail",
            "text_parts": {
                "committed_prefix": "", "current_clause": "正在播报",
                "prefetched_clause": "", "revisable_tail": "",
            },
        }),
        context_kind="runtime_state",
    ))
    messages = consolidate_request_tail(messages)
    assert _current_input(messages) == {
        "type": CURRENT_USER_INPUT_TYPE, "version": 1, "content": "现在精确到秒是多少",
    }
    assert messages[-3]["context_kind"] == VOICE_REQUEST_CONTEXT_TYPE
    assert messages[-2]["context_kind"] == SPEECH_QUEUE_TASK_KIND
    assert messages[-2]["content"] == SPEECH_QUEUE_TASK
    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert '"local_time"' not in messages[0]["content"]
    packet = _request_packet(messages)
    assert packet["type"] == VOICE_REQUEST_CONTEXT_TYPE
    assert packet["version"] == 2
    assert packet["runtime"]["clock"]["local_time"]
    assert packet["speech"]["locked_text"] == "正在播报"
    assert packet["speech"]["editable_text"] == ""


def test_runtime_snapshot_projects_full_long_locked_and_editable_text():
    locked = ("已经锁定的完整上下文。" * 90) + "当前未结束的半句"
    editable = ("后续仍有效的队列内容；" * 70) + "请继续。"
    current = CurrentInput(text="补充一个事实", primary_transcript="补充一个事实")
    rendered = LLMContextProjector.runtime_tail(
        current=current,
        speak_state={
            "state": "speaking",
            "text_parts": {
                "committed_prefix": locked[:-7],
                "current_clause": locked[-7:],
                "prefetched_clause": "",
                "revisable_tail": editable,
            },
        },
    )
    messages = consolidate_request_tail([
        ContextFrame.make(ContextLane.REQUEST_TAIL, "user", current.text,
                          context_kind=CURRENT_USER_INPUT_TYPE),
        ContextFrame.make(ContextLane.REQUEST_TAIL, "system", rendered,
                          context_kind="runtime_state"),
    ])

    packet = _request_packet(messages)
    assert packet["speech"]["locked_text"] == locked
    assert packet["speech"]["editable_text"] == editable
    assert "text_parts" not in packet["speech"]
    assert packet["speech"]["locked_text"] + packet["speech"]["editable_text"] == locked + editable


def test_half_sentence_current_input_and_snapshot_continuation_are_verbatim():
    current_text = "我想把预算提高到五千，然后先等等"
    locked = "安排已确认，先保留这句尚未结束的说明，"
    editable = "后续再补上地点和时间。"
    current = CurrentInput(text=current_text, primary_transcript=current_text)
    messages = consolidate_request_tail([
        *LLMContextProjector().project(SessionLedger(), current),
        _runtime_state_frame({
            "runtime": {"clock": {"local_time": "12:00:00", "precision": "second"}},
            "speech": {"state": "speaking", "locked_text": locked, "editable_text": editable},
        }),
    ])

    assert _current_input(messages)["content"] == current_text
    packet = _request_packet(messages)
    assert packet["speech"]["locked_text"] == locked
    assert packet["speech"]["editable_text"] == editable


def test_dynamic_speak_parts_change_only_request_tail_and_keep_cache_prefix_frozen():
    class Registry:
        status = "READY"

        def action_definitions(self):
            return [{
                "name": "lookup", "description": "读取信息",
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False},
            }]

        def begin_turn(self, _turn_id):
            return None

        def service_status(self, _name):
            return self.status

    async def emit(*_args):
        return None

    output = SimpleNamespace(speak_snapshot={
        "queue_status": "speaking",
        "conversation_id": "conversation",
        "turn_id": "turn-a",
        "text": "第一句。第二句。第三句。",
        "sent_chars": len("第一句。第二句。"),
        "rewritable": True,
        "_model_text_parts_valid": True,
        "_model_text_parts": {
            "committed_prefix": "", "current_clause": "第一句。",
            "prefetched_clause": "第二句。", "revisable_tail": "第三句。",
        },
    })
    conversation = ConversationRuntime()
    conversation.begin_turn("turn-a")
    ledger = SessionLedger()
    registry = Registry()
    runtime = AgentEpisodeRuntime(
        None, registry, output, emit,
        conversation=conversation, ledger=ledger, reasoning_mode="fast",
    )
    runtime.episode_id = "conversation"
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "继续", "final": False}

    before = runtime.messages({"USER_SEGMENT_UPDATED"}, current_turn_id="turn-a")
    runtime._guard_context_contract(before)
    static_before = [item for item in before if ContextFrame.lane_of(item) == ContextLane.STATIC_INSTRUCTIONS]
    history_before = [item for item in before if ContextFrame.lane_of(item) == ContextLane.HISTORY]
    static_digest = runtime.static_context_digest
    history_digest = runtime.history_context_digest

    output.speak_snapshot = {
        **output.speak_snapshot,
        "sent_chars": len("第一句。第二句。第三句。"),
        "rewritable": False,
        "_model_text_parts": {
            "committed_prefix": "第一句。", "current_clause": "第二句。",
            "prefetched_clause": "第三句。", "revisable_tail": "",
        },
    }
    runtime.reasoning_mode = "deep"  # reasoning_mode（推理模式）变化也只能追加动态帧。
    registry.status = "BUSY"
    ledger.set_shadow_runtime_state(available=True, active=True)
    after = runtime.messages(
        {"TOOL_COMPLETED"}, current_turn_id="turn-a",
        shadow_information={"text": "后台发现新的来源", "source": "observer"},
    )
    runtime._guard_context_contract(after)
    assert [item for item in after if ContextFrame.lane_of(item) == ContextLane.STATIC_INSTRUCTIONS] == static_before
    assert [item for item in after if ContextFrame.lane_of(item) == ContextLane.HISTORY] == history_before
    assert runtime.static_context_digest == static_digest
    assert runtime.history_context_digest == history_digest
    assert any(ContextFrame.kind_of(item) == "reasoning_mode" for item in after)
    packet_before = _request_packet(before)
    packet = _request_packet(after)
    assert packet_before["speech"]["locked_text"] == "第一句。第二句。"
    assert packet_before["speech"]["editable_text"] == "第三句。"
    assert packet["speech"]["locked_text"] == "第一句。第二句。第三句。"
    assert packet["speech"]["editable_text"] == ""
    assert packet["speech"]["continuation_mode"] == "none"
    assert "text_parts" not in packet["speech"]
    assert packet_before["tool_availability"] == {"lookup": "ready"}
    assert packet["tool_availability"] == {"lookup": "busy"}
    assert packet["shadow"]["active"] is True
    assert packet["shadow"]["information"] == {
        "text": "后台发现新的来源", "source": "observer",
    }
    assert packet["runtime"]["clock"]["local_time"]
    assert "第一句。" not in "".join(item["content"] for item in static_before)
    assert "第三句。" not in "".join(item["content"] for item in history_before)


def test_request_tail_consolidation_is_lossless_ordered_and_final():
    source = [
        ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "rules"),
        ContextFrame.make(ContextLane.HISTORY, "assistant", "heard"),
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "user", "  用户原文\n第二行  ",
            context_kind="current_user_input",
        ),
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "恢复原文", context_kind="immediate_recovery",
        ),
        _runtime_state_frame({"runtime": {"clock": {"local_time": "12:00:00"}},
                              "speech": {"state": "empty"}}),
    ]

    messages = consolidate_request_tail(source)

    assert messages[:2] == source[:2]
    assert _current_input(messages) == {
        "type": "current_user_input", "version": 1, "content": "  用户原文\n第二行  ",
    }
    kinds = [ContextFrame.kind_of(frame) for frame in messages]
    assert kinds[-5:] == [
        CURRENT_USER_INPUT_TYPE, "immediate_recovery", VOICE_REQUEST_CONTEXT_TYPE,
        SPEECH_QUEUE_TASK_KIND, "output_constraint",
    ]
    packet = _request_packet(messages)
    assert packet == {
        "type": VOICE_REQUEST_CONTEXT_TYPE,
        "version": 2,
        "runtime": {"clock": {"local_time": "12:00:00"}},
        "speech": {"state": "empty"},
    }
    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert messages[-1]["context_kind"] == "output_constraint"


def test_request_tail_without_current_user_does_not_invent_one():
    messages = consolidate_request_tail([
        ContextFrame.make(ContextLane.HISTORY, "user", "committed"),
        _runtime_state_frame({"runtime": {}, "speech": {"state": "empty"}}),
    ])
    assert [frame["role"] for frame in messages] == ["user", "system", "system", "system"]
    assert not any(ContextFrame.kind_of(frame) == CURRENT_USER_INPUT_TYPE for frame in messages)
    assert _request_packet(messages)["speech"] == {"state": "empty"}
    assert messages[-2]["content"] == SPEECH_QUEUE_TASK
    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT


def test_context_guard_rejects_any_frame_after_output_constraint():
    episode = AgentEpisodeRuntime(
        None, object(), object(), lambda *_args: None,
        conversation=ConversationRuntime(), ledger=SessionLedger(), reasoning_mode="fast",
    )
    messages = consolidate_request_tail([
        _runtime_state_frame({"runtime": {}, "speech": {"state": "empty"}}),
    ])
    messages.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL, "system", "late", context_kind="system_context",
    ))
    with pytest.raises(RuntimeError, match="output constraint"):
        episode._guard_context_contract(messages)


def test_runtime_classifies_recovery_and_runtime_without_loss():
    class Registry:
        def action_definitions(self):
            return []

        def service_status(self, _name):
            return "READY"

    output = SimpleNamespace(
        speak_snapshot={"queue_status": "empty", "text_only_queue": True},
    )
    runtime = AgentEpisodeRuntime(
        None, Registry(), output, lambda *_args: None,
        conversation=ConversationRuntime(), ledger=SessionLedger(), reasoning_mode="fast",
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.user_speaking = True
    runtime.inputs["turn-a"] = {"text": "当前输入", "final": False}
    signal = RecoverySignal("SPEAK_ENVELOPE_INVALID", "bad", source_request_id="request-a")

    messages = runtime.messages({"EXCEPTION_RECOVERY"}, recovery_signal=signal)
    packet = _request_packet(messages)
    recovery = next(frame for frame in messages
                    if ContextFrame.kind_of(frame) == "immediate_recovery")

    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert ContextFrame.kind_of(messages[-2]) == SPEECH_QUEUE_TASK_KIND
    assert packet["version"] == 2
    assert packet["runtime"]["clock"]["local_time"]
    assert packet["speech"]["state"] == "empty"
    assert packet["speech"]["locked_text"] == ""
    assert packet["speech"]["editable_text"] == ""
    assert "SPEAK_ENVELOPE_INVALID" in recovery["content"]

    runtime.minor_protocol_feedback.append({"field": "emotion"})
    minor_messages = runtime.messages({"USER_INPUT_FINALIZED"})
    minor = next(frame for frame in minor_messages
                 if ContextFrame.kind_of(frame) == "minor_protocol_feedback")
    assert _request_packet(minor_messages)["runtime"]["input_status"] == "speaking"
    assert "emotion" in minor["content"]

    runtime.deferred_recovery_feedback.append(signal)
    deferred_messages = runtime.messages({"USER_INPUT_FINALIZED"})
    deferred = next(frame for frame in deferred_messages
                    if ContextFrame.kind_of(frame) == "deferred_recovery")
    assert _request_packet(deferred_messages)["version"] == 2
    assert "SPEAK_ENVELOPE_INVALID" in deferred["content"]
    assert "已回到普通事件调度" not in deferred["content"]
    assert "重新处理最后一条用户输入" not in deferred["content"]


@pytest.mark.parametrize("prompt_kind", ["normal", "secondary_mailbox", "final_silence", "kws_wake"])
@pytest.mark.parametrize("recovering", [False, True])
def test_queue_policy_survives_context_fit_and_is_last_before_output_guard(prompt_kind, recovering):
    """真实消息组装、裁剪及供应商投影都必须保留请求末尾的队列规则。"""
    class Registry:
        def action_definitions(self):
            return []

    output = SimpleNamespace(speak_snapshot={"queue_status": "empty", "text_only_queue": True})
    runtime = AgentEpisodeRuntime(
        None, Registry(), output, lambda *_args: None,
        options=AgentOptions(max_context_tokens=128),
        conversation=ConversationRuntime(), ledger=SessionLedger(), reasoning_mode="fast",
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "需要回答的新问题" * 512, "final": True}
    signal = (RecoverySignal(
        "SPEAK_ENVELOPE_INVALID", "bad", source_request_id="request-a",
        dispatch_context={"origin_prompt_kind": prompt_kind},
    ) if recovering else None)
    messages = runtime.messages(
        {"EXCEPTION_RECOVERY"} if recovering else {"USER_INPUT_FINALIZED"},
        recovery_signal=signal, prompt_kind=prompt_kind, reasoning_effort="low",
    )
    fitted, changes = runtime._fit_context(messages, [])
    assert changes["truncated_current_input"]
    payload = DeepSeekClient._responses_payload(
        fitted, model="deepseek-flash", schema=decision_schema([]),
        schema_name="voice_decision_v06", reasoning_effort="low", max_output_tokens=100,
    )
    assert SPEECH_QUEUE_DECISION_PROMPT in payload["instructions"]
    packet_item = next(item for item in payload["input"]
                       if VOICE_REQUEST_CONTEXT_TYPE in item["content"][0]["text"])
    packet = json.loads(packet_item["content"][0]["text"])
    assert packet["type"] == VOICE_REQUEST_CONTEXT_TYPE and packet["version"] == 2
    assert isinstance(packet["runtime"], dict) and isinstance(packet["speech"], dict)
    assert payload["input"][-2]["content"][0]["text"] == SPEECH_QUEUE_TASK
    details = [ContextFrame.kind_of(frame) for frame in fitted
               if ContextFrame.lane_of(frame) == ContextLane.REQUEST_TAIL]
    assert SPEECH_QUEUE_TASK_KIND in details
    assert "runtime_state" not in details  # runtime_state（运行状态）已原生投影为 JSON 快照。
    if prompt_kind != "normal":
        assert prompt_kind in details
    if recovering:
        assert "immediate_recovery" in details
    assert payload["input"][-1]["content"][0]["text"] == FINAL_OUTPUT_CONSTRAINT


def test_fit_context_locates_typed_current_input_before_final_constraint():
    episode = AgentEpisodeRuntime(
        None, object(), object(), lambda *_args: None,
        options=AgentOptions(max_context_tokens=128),
        conversation=ConversationRuntime(), ledger=SessionLedger(), reasoning_mode="fast",
    )
    messages = consolidate_request_tail([
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "user", "x" * 4096,
            context_kind="current_user_input",
        ),
        _runtime_state_frame({"runtime": {}, "speech": {"state": "empty"}}),
    ])

    fitted, changes = episode._fit_context(messages, [])

    current = _current_input(fitted)
    assert changes["truncated_current_input"] is True
    assert current["content"].startswith("…[truncated]")
    assert len(current["content"]) <= 1024
    assert fitted[-3]["context_kind"] == VOICE_REQUEST_CONTEXT_TYPE
    assert fitted[-2]["content"] == SPEECH_QUEUE_TASK
    assert fitted[-1]["content"] == FINAL_OUTPUT_CONSTRAINT


def test_calendar_epoch_persists_after_midnight_until_session_reset():
    runtime = ConversationRuntime()
    runtime._calendar_baseline_day = "1900-01-01"  # simulate a live session crossing midnight
    runtime._calendar_day = "1900-01-01"
    first = runtime.calendar_epoch_if_changed()
    second = runtime.calendar_epoch_if_changed()
    assert first is not None and "当前日期 epoch" in first
    assert second is not None and "当前日期 epoch" in second


def test_usage_normalization_keeps_missing_cache_metrics_missing():
    official = DeepSeekClient._normalize_usage({
        "input_tokens": 100, "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 64},
    })
    assert official["prompt_tokens"] == 100
    assert official["cached_tokens"] == 64
    assert official["cache_miss_tokens"] == 36
    relay = DeepSeekClient._normalize_usage({"prompt_tokens": 20, "prompt_cache_hit_tokens": 12})
    assert relay["cached_tokens"] == 12 and relay["cache_miss_tokens"] == 8
    unavailable = DeepSeekClient._normalize_usage({"prompt_tokens": 20})
    assert "cached_tokens" not in unavailable and "cache_miss_tokens" not in unavailable


def test_context_guards_reject_history_rewrite_and_static_drift():
    class Registry:
        def action_definitions(self):
            return []

    async def emit(*_args):
        return None

    episode = AgentEpisodeRuntime(None, Registry(), object(), emit,
                                  conversation=ConversationRuntime(), ledger=SessionLedger(),
                                  reasoning_mode="fast")
    initial = consolidate_request_tail([
        ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "rules"),
        ContextFrame.make(ContextLane.HISTORY, "user", "one"),
        _runtime_state_frame({"runtime": {}, "speech": {"state": "empty"}}),
    ])
    episode._guard_context_contract(initial)
    extended = initial[:2] + [ContextFrame.make(ContextLane.HISTORY, "assistant", "heard")] + initial[2:]
    episode._guard_context_contract(extended)
    rewritten = extended.copy()
    rewritten[1] = ContextFrame.make(ContextLane.HISTORY, "user", "changed")
    with pytest.raises(RuntimeError, match="history"):
        episode._guard_context_contract(rewritten)


def test_late_refinement_does_not_rewrite_cached_history_prefix():
    """The production P0 sequence must remain cache-prefix safe.

    A Paraformer final can be used to answer the user before SenseVoice
    returns.  Once an assistant delivery has followed it, the late refinement
    must be request-tail evidence for that same turn, never a mutation of the
    earlier user HISTORY frame.
    """

    class Registry:
        def action_definitions(self):
            return []

    async def emit(*_args):
        return None

    ledger = SessionLedger()
    projector = LLMContextProjector()
    episode = AgentEpisodeRuntime(
        None, Registry(), object(), emit,
        conversation=ConversationRuntime(), ledger=ledger, reasoning_mode="fast",
    )
    before_current = CurrentInput(
        text="初始转录", primary_transcript="初始转录",
        finalized=True, turn_id="turn-a",
        transcript_updates=(
            {"kind": "asr_final", "source": "paraformer", "text": "初始转录", "revision": 1},
        ),
    )

    def request(current: CurrentInput):
        return consolidate_request_tail([
            ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "stable rules"),
            *projector.project(ledger, current),
            ContextFrame.make(
                ContextLane.REQUEST_TAIL, "system",
                projector.runtime_tail(current=current, speak_state={"state": "empty"}),
                context_kind="runtime_state",
            ),
        ])

    ledger.commit_input("turn-a", "初始转录")
    before = request(before_current)
    episode._guard_context_contract(before)
    before_history = [frame for frame in before if frame["context_lane"] == ContextLane.HISTORY]

    ledger.commit_delivery("speech-a", "已经完整播出的回答", DeliveryReport(
        status="full", termination="completed", heard_text="", precision="playback_complete",
    ))
    ledger.update_input_refinement("turn-a", "晚到精修转录")
    after_current = CurrentInput(
        text="初始转录", primary_transcript="初始转录",
        finalized=True, turn_id="turn-a", recent_events=("USER_INPUT_REFINED",),
        transcript_updates=(
            {"kind": "asr_final", "source": "paraformer", "text": "初始转录", "revision": 1},
            {"kind": "refinement", "source": "sensevoice", "text": "晚到精修转录", "revision": 2,
             "audio_cues": [{"start_ms": 0, "end_ms": 900, "text": "晚到精修转录",
                             "language": "zh", "emotion": "HAPPY",
                             "events": ["Speech", "BGM"], "raw_tags": ["HAPPY", "BGM"]}]},
        ),
    )
    after = request(after_current)

    # Regression assertion: this used to raise
    # ``existing history frames changed during a session`` here.
    episode._guard_context_contract(after)
    after_history = [frame for frame in after if frame["context_lane"] == ContextLane.HISTORY]
    assert after_history[:1] == before_history
    assert after_history[1]["role"] == "assistant"
    assert after_history[1]["content"] == (
        '<voice_delivery status="full">已经完整播出的回答</voice_delivery>'
    )
    assert '"refinement":"晚到精修转录"' not in after_history[0]["content"]
    packet = _request_packet(after)
    runtime_content = json.dumps(packet["runtime"], ensure_ascii=False, separators=(",", ":"))
    assert '"kind":"refinement"' in runtime_content
    assert '"text":"晚到精修转录"' in runtime_content
    assert '"triggered_by":["refinement"]' in runtime_content
    assert '"audio_cues":[{"start_ms":0,"end_ms":900,"emotion":"HAPPY","events":["BGM"]}]' in runtime_content
    assert "raw_tags" not in runtime_content
    assert "language" not in runtime_content
    provider = DeepSeekClient._responses_payload(
        after, model="deepseek", schema=decision_schema([]), schema_name="decision",
        reasoning_effort="none", max_output_tokens=100,
    )
    assert "audio_cues" in json.dumps(provider["input"], ensure_ascii=False)

    later = request(CurrentInput(
        text="初始转录", primary_transcript="初始转录", finalized=True,
        turn_id="turn-a", transcript_updates=after_current.transcript_updates,
    ))
    assert '"audio_cues"' not in json.dumps(later, ensure_ascii=False)


def test_context_fit_preserves_oversized_locked_and_editable_snapshot_atomically():
    locked = ("不可更改的锁定正文。" * 900) + "尚未结束，"
    editable = ("仍然有效的旧尾巴；" * 800) + "最后一句。"
    text = locked + editable
    output = SimpleNamespace(speak_snapshot={
        "queue_status": "speaking",
        "conversation_id": "conversation",
        "turn_id": "turn-a",
        "text": text,
        "sent_chars": len(locked),
        "rewritable": True,
        "_model_text_parts_valid": True,
        "_model_text_parts": {
            "committed_prefix": locked[:len(locked) // 3],
            "current_clause": locked[len(locked) // 3:2 * len(locked) // 3],
            "prefetched_clause": locked[2 * len(locked) // 3:],
            "revisable_tail": editable,
        },
    })
    conversation = ConversationRuntime()
    conversation.begin_turn("turn-a")
    episode = AgentEpisodeRuntime(
        None, object(), output, lambda *_args: None,
        options=AgentOptions(max_context_tokens=128), conversation=conversation,
        ledger=SessionLedger(), reasoning_mode="fast",
    )
    episode.episode_id = "conversation"
    episode.last_turn_id = "turn-a"
    episode.inputs["turn-a"] = {"text": "请保留上下文", "final": False}
    messages = episode.messages({"USER_SEGMENT_UPDATED"}, current_turn_id="turn-a")

    fitted, changes = episode._fit_context(messages, [])

    packet = _request_packet(fitted)
    assert changes["over_budget_base"] is True
    assert changes["truncated_current_input"] is False
    assert packet["speech"]["locked_text"] == locked
    assert packet["speech"]["editable_text"] == editable
    assert packet["speech"]["locked_text"] + packet["speech"]["editable_text"] == text
