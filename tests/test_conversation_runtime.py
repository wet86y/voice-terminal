from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.contracts import ToolExchange
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime, EpochReason
from voice_terminal.agent.episode import AgentEpisodeRuntime


def test_epoch_invalidates_old_turn_and_reset_changes_conversation() -> None:
    runtime = ConversationRuntime()
    original = runtime.conversation_id
    runtime.begin_turn("turn_a")
    assert runtime.is_current("turn_a", 0)
    runtime.advance_epoch(EpochReason.USER_RESUMED)
    assert not runtime.is_current("turn_a", 0)
    assert runtime.is_current("turn_a", 1)
    runtime.reset()
    assert runtime.conversation_id != original
    assert not runtime.is_current("turn_a", 1)


def test_context_projection_excludes_runtime_identity_and_keeps_semantics() -> None:
    ledger = SessionLedger()
    ledger.commit_input("turn_a", "查上海天气")
    ledger.commit_tool(ToolExchange("web_search", {"q": "上海天气"}, {"text": "晴"}, "now",
                                    "conversation-secret", "decision-secret", 17))
    messages = LLMContextProjector().project(ledger, CurrentInput("改成明天", True, False, ("input.finalized",)))
    rendered = "\n".join(str(message["content"]) for message in messages)
    assert "conversation-secret" not in rendered
    assert "decision-secret" not in rendered
    assert "17" not in rendered
    assert "查上海天气" in rendered and "改成明天" in rendered


def test_request_local_tool_projection_strips_runtime_ids() -> None:
    message = AgentEpisodeRuntime._project_protocol_message({
        "role": "tool", "tool_call_id": "call_1",
        "content": '{"status":"accepted","speech_id":"speech_secret","epoch":9,"turn_id":"turn_secret"}',
    })
    assert "speech_secret" not in message["content"]
    assert "turn_secret" not in message["content"]
    assert '"accepted"' in message["content"]
