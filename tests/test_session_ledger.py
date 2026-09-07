import json

from voice_terminal.session.contracts import DeliveryReport, ToolExchange
from voice_terminal.session.ledger import SessionLedger


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
    assert messages[-1] == {"role": "user", "content": "继续"}


def test_reset_removes_all_session_memory():
    ledger = SessionLedger()
    ledger.commit_input("input_a", "内容")
    ledger.reset()
    assert len(ledger.context_messages("")) == 1
