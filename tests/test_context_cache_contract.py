from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime, RecoverySignal
from voice_terminal.agent.options import AgentOptions
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context import (
    FINAL_OUTPUT_CONSTRAINT, CurrentInput, LLMContextProjector,
    consolidate_request_tail,
)
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


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
        ContextLane.REQUEST_TAIL, "system",
        projector.runtime_tail(current=current, speak_state={"state": "speaking", "target_text": "正在播报"}),
        context_kind="runtime_state",
    ))
    messages = consolidate_request_tail(messages)
    assert messages[-3]["role"] == "user"
    assert json.loads(messages[-3]["content"])["content"] == "现在精确到秒是多少"
    assert messages[-2]["context_kind"] == "voice_request_context"
    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert '"local_time"' not in messages[0]["content"]
    packet = json.loads(messages[-2]["content"])
    assert '"local_time"' in packet["entries"][0]["content"]


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
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "状态原文", context_kind="runtime_state",
        ),
    ]

    messages = consolidate_request_tail(source)

    assert messages[:2] == source[:2]
    assert json.loads(messages[-3]["content"]) == {
        "type": "current_user_input", "version": 1, "content": "  用户原文\n第二行  ",
    }
    packet = json.loads(messages[-2]["content"])
    assert packet == {
        "type": "voice_request_context",
        "version": 1,
        "entries": [
            {"index": 0, "kind": "immediate_recovery", "content": "恢复原文"},
            {"index": 1, "kind": "runtime_state", "content": "状态原文"},
        ],
    }
    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert messages[-1]["context_kind"] == "output_constraint"


def test_request_tail_without_current_user_does_not_invent_one():
    messages = consolidate_request_tail([
        ContextFrame.make(ContextLane.HISTORY, "user", "committed"),
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "state", context_kind="runtime_state",
        ),
    ])
    assert [frame["role"] for frame in messages] == ["user", "system", "system"]
    assert json.loads(messages[-2]["content"])["entries"][0]["content"] == "state"


def test_context_guard_rejects_any_frame_after_output_constraint():
    episode = AgentEpisodeRuntime(
        None, object(), object(), lambda *_args: None,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )
    messages = consolidate_request_tail([
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "state", context_kind="runtime_state",
        ),
    ])
    messages.append(ContextFrame.make(
        ContextLane.REQUEST_TAIL, "system", "late", context_kind="system_context",
    ))
    with pytest.raises(RuntimeError, match="output constraint"):
        episode._guard_context_contract(messages)


def test_runtime_classifies_recovery_pending_tools_and_runtime_without_loss():
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
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "当前输入", "final": False}
    runtime._thinking_pending_by_turn["turn-a"] = [{"name": "search"}]
    signal = RecoverySignal("SPEAK_ENVELOPE_INVALID", "bad", source_request_id="request-a")

    messages = runtime.messages({"EXCEPTION_RECOVERY"}, recovery_signal=signal)
    packet = json.loads(messages[-2]["content"])

    assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
    assert [entry["kind"] for entry in packet["entries"]] == [
        "immediate_recovery", "runtime_state", "pending_tools",
    ]
    assert "SPEAK_ENVELOPE_INVALID" in packet["entries"][0]["content"]
    assert "当前轮运行状态" in packet["entries"][1]["content"]
    assert "search" in packet["entries"][2]["content"]

    runtime._thinking_pending_by_turn.clear()
    runtime.minor_protocol_feedback.append({"field": "emotion"})
    minor = json.loads(runtime.messages({"USER_INPUT_FINALIZED"})[-2]["content"])
    assert [entry["kind"] for entry in minor["entries"]] == [
        "minor_protocol_feedback", "runtime_state",
    ]
    assert "emotion" in minor["entries"][0]["content"]

    runtime.deferred_recovery_feedback.append(signal)
    deferred = json.loads(runtime.messages({"USER_INPUT_FINALIZED"})[-2]["content"])
    assert [entry["kind"] for entry in deferred["entries"]] == [
        "deferred_recovery", "runtime_state",
    ]
    assert "SPEAK_ENVELOPE_INVALID" in deferred["entries"][0]["content"]


def test_fit_context_locates_typed_current_input_before_final_constraint():
    episode = AgentEpisodeRuntime(
        None, object(), object(), lambda *_args: None,
        options=AgentOptions(max_context_tokens=128),
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )
    messages = consolidate_request_tail([
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "user", "x" * 4096,
            context_kind="current_user_input",
        ),
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "state", context_kind="runtime_state",
        ),
    ])

    fitted, changes = episode._fit_context(messages, [])

    current = json.loads(fitted[-3]["content"])
    assert changes["truncated_current_input"] is True
    assert current["content"].startswith("…[truncated]")
    assert len(current["content"]) <= 1024
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
                                  conversation=ConversationRuntime(), ledger=SessionLedger())
    initial = consolidate_request_tail([
        ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "rules"),
        ContextFrame.make(ContextLane.HISTORY, "user", "one"),
        ContextFrame.make(
            ContextLane.REQUEST_TAIL, "system", "tail", context_kind="runtime_state",
        ),
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
        conversation=ConversationRuntime(), ledger=ledger,
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
            {"kind": "refinement", "source": "sensevoice", "text": "晚到精修转录", "revision": 2},
        ),
    )
    after = request(after_current)

    # Regression assertion: this used to raise
    # ``existing history frames changed during a session`` here.
    episode._guard_context_contract(after)
    after_history = [frame for frame in after if frame["context_lane"] == ContextLane.HISTORY]
    assert after_history[:1] == before_history
    assert after_history[1]["role"] == "assistant"
    assert after_history[1]["content"] == "已经完整播出的回答"
    assert '"refinement":"晚到精修转录"' not in after_history[0]["content"]
    packet = json.loads(after[-2]["content"])
    runtime_content = packet["entries"][0]["content"]
    assert '"kind":"refinement"' in runtime_content
    assert '"text":"晚到精修转录"' in runtime_content
    assert '"triggered_by":["refinement"]' in runtime_content
