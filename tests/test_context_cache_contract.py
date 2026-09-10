from __future__ import annotations

import pytest

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_responses_projects_only_static_frames_to_instructions_and_hides_lanes():
    payload = DeepSeekClient._responses_payload(
        [
            ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "stable rules"),
            ContextFrame.make(ContextLane.HISTORY, "assistant", "heard text"),
            ContextFrame.make(ContextLane.REQUEST_TAIL, "system", "live state"),
        ],
        model="deepseek", schema=decision_schema([]), schema_name="decision",
        reasoning_effort="none", max_output_tokens=100,
    )
    assert payload["instructions"].startswith("stable rules")
    assert [item["role"] for item in payload["input"]] == ["assistant", "system"]
    assert [item["content"][0]["text"] for item in payload["input"]] == ["heard text", "live state"]
    assert "context_lane" not in str(payload)


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
    ))
    assert messages[-2]["role"] == "user"
    assert messages[-1]["context_lane"] == ContextLane.REQUEST_TAIL
    assert '"local_time"' not in messages[0]["content"]
    assert '"local_time"' in messages[-1]["content"]


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
    initial = [
        ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "rules"),
        ContextFrame.make(ContextLane.HISTORY, "user", "one"),
        ContextFrame.make(ContextLane.REQUEST_TAIL, "system", "tail"),
    ]
    episode._guard_context_contract(initial)
    extended = initial[:2] + [ContextFrame.make(ContextLane.HISTORY, "assistant", "heard")] + initial[2:]
    episode._guard_context_contract(extended)
    rewritten = extended.copy()
    rewritten[1] = ContextFrame.make(ContextLane.HISTORY, "user", "changed")
    with pytest.raises(RuntimeError, match="history"):
        episode._guard_context_contract(rewritten)
