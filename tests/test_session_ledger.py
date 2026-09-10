import json

from voice_terminal.session.contracts import DeliveryReport, ToolExchange
from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.context_frames import ContextLane


def test_ledger_injects_only_heard_speech_and_structured_tool_source():
    ledger = SessionLedger()
    ledger.commit_input("input_a", "查一下天气")
    ledger.commit_delivery("speech_a", "完整回答", DeliveryReport(
        status="partial", termination="interrupted", heard_text="前半句", unheard_text="后半句"))
    ledger.commit_tool(ToolExchange("web_search", {"query": "天气"}, {"items": []}, "now",
                                     "episode_a", "decision_a", 2))
    messages = ledger.context_messages("继续")
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "前半句" in serialized
    assert "完整回答" not in serialized
    assert "external_untrusted" in serialized
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "继续"
    assert messages[-1]["context_lane"] == ContextLane.HISTORY


def test_reset_removes_all_session_memory():
    ledger = SessionLedger()
    ledger.commit_input("input_a", "内容")
    ledger.reset()
    assert ledger.context_messages("") == []


def test_context_projects_current_input_as_tail_and_runtime_time_excludes_scheduler_events():
    current = CurrentInput(
        text="现在几点", user_speaking=True,
        recent_events=("SPEECH_STARTED", "USER_SEGMENT_UPDATED"),
    )
    messages = LLMContextProjector().project(SessionLedger(), current)
    rendered = LLMContextProjector.runtime_tail(current=current, speak_state={"state": "empty"})
    assert messages[-1]["context_lane"] == ContextLane.REQUEST_TAIL
    assert messages[-1]["content"] == "现在几点"
    assert '"local_time"' in rendered and '"precision":"second"' in rendered
    assert '"input_status":"speaking"' in rendered
    assert "SPEECH_STARTED" not in rendered
