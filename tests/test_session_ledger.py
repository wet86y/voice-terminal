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
    assert '<voice_delivery status=\\"partial\\" termination=\\"interrupted\\">' in serialized
    assert "<heard>前半句</heard>" in serialized
    assert "<unheard>后半句</unheard>" in serialized
    assert "上一段语音被打断" not in serialized
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


def test_late_refinement_keeps_user_history_frozen_and_exposes_runtime_update_object():
    ledger = SessionLedger()
    ledger.commit_input("turn-a", "明天开会")
    initial_history = ledger.context_messages("")
    ledger.update_input_refinement("turn-a", "明天下午开会")

    messages = ledger.context_messages("")
    user_messages = [item for item in messages if item["role"] == "user"]
    assert len(user_messages) == 1
    assert json.loads(user_messages[0]["content"].removeprefix("用户语音最终文本：")) == {
        "asr_final": "明天开会",
    }
    assert user_messages[0] == initial_history[0]
    assert not any(item["role"] == "system" and "精修补充" in item["content"] for item in messages)

    current = CurrentInput(
        text="明天开会", finalized=True, turn_id="turn-a",
        recent_events=("USER_INPUT_REFINED",),
        transcript_updates=(
            {"kind": "asr_final", "source": "paraformer", "text": "明天开会", "revision": 1},
            {"kind": "refinement", "source": "sensevoice", "text": "明天下午开会", "revision": 2},
        ),
    )
    runtime = LLMContextProjector.runtime_tail(current=current, speak_state={"state": "empty"})
    assert '"same_user_turn":true' in runtime
    assert '"triggered_by":["refinement"]' in runtime
    assert '"kind":"refinement"' in runtime
    assert '"text":"明天下午开会"' in runtime


def test_refinement_available_when_input_commits_is_part_of_frozen_history_only_once():
    ledger = SessionLedger()
    ledger.commit_input("turn-a", "明天开会", details={"refined_text": "明天下午开会"})
    initial_history = ledger.context_messages("")

    ledger.update_input_refinement("turn-a", "明天上午开会")

    user_messages = [item for item in ledger.context_messages("") if item["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0] == initial_history[0]
    assert json.loads(user_messages[0]["content"].removeprefix("用户语音最终文本：")) == {
        "asr_final": "明天开会",
        "refinement": "明天下午开会",
    }
    assert ledger.input_refinement("turn-a") == "明天上午开会"


def test_full_delivery_commits_original_speech_once_even_if_alignment_heard_text_is_empty():
    ledger = SessionLedger()
    delivery = DeliveryReport(
        status="full", termination="completed", heard_text="", precision="playback_complete",
    )

    assert ledger.commit_delivery("speech-a", "完整回答", delivery) is True
    assert ledger.commit_delivery("speech-a", "重复写入", delivery) is False
    assistant = [entry for entry in ledger.entries if entry.kind == "speech"]
    assert len(assistant) == 1
    assert assistant[0].content == "完整回答"
    assert assistant[0].metadata["model_history_format"] == "voice_delivery_v1"
    assert assistant[0].metadata["model_content"] == (
        '<voice_delivery status="full">完整回答</voice_delivery>'
    )
    messages = ledger.context_messages("")
    assert messages == [{
        "role": "assistant",
        "content": '<voice_delivery status="full">完整回答</voice_delivery>',
        "context_lane": ContextLane.HISTORY,
    }]
    assert ledger.has_speech("speech-a") is True


def test_voice_delivery_content_is_frozen_and_xml_escaped():
    ledger = SessionLedger()
    delivery = DeliveryReport(
        status="full", termination="completed", precision="playback_complete",
    )

    ledger.commit_delivery("speech-a", 'A < B & C 说“好”', delivery)
    entry = next(item for item in ledger.entries if item.kind == "speech")
    frozen = entry.metadata["model_content"]

    assert frozen == (
        '<voice_delivery status="full">A &lt; B &amp; C 说“好”</voice_delivery>'
    )
    assert ledger.context_messages("")[0]["content"] == frozen
