from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.contracts import ToolExchange
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.session.context_frames import ContextLane


def test_explicit_scope_changes_only_on_turn_or_cancel_and_reset_changes_conversation() -> None:
    runtime = ConversationRuntime()
    original = runtime.conversation_id
    first = runtime.begin_turn("turn_a")
    assert runtime.is_scope_current(first.scope_id, "turn_a")
    cancelled = runtime.cancel(turn_id="turn_a", reason="barge_in")
    assert cancelled.scope_id != first.scope_id
    assert runtime.is_scope_current(cancelled.scope_id, "turn_a")
    runtime.reset()
    assert runtime.conversation_id != original
    assert not runtime.is_scope_current(cancelled.scope_id, "turn_a")


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


def test_context_projection_does_not_duplicate_committed_current_turn() -> None:
    ledger = SessionLedger()
    ledger.commit_input("turn_a", "CURRENT_INPUT_SENTINEL", details={"refined_text": "精修文本"})
    messages = LLMContextProjector().project(ledger, CurrentInput(
        text="CURRENT_INPUT_SENTINEL",
        primary_transcript="CURRENT_INPUT_SENTINEL",
        refined_transcript="精修文本",
        refinement_available=True,
        finalized=True,
        turn_id="turn_a",
    ))
    rendered = "\n".join(str(message["content"]) for message in messages)
    assert rendered.count("CURRENT_INPUT_SENTINEL") == 1
    assert rendered.count("精修文本") == 1


def test_context_projection_keeps_identical_text_for_a_new_turn() -> None:
    ledger = SessionLedger()
    ledger.commit_input("turn_a", "重复文本")
    messages = LLMContextProjector().project(ledger, CurrentInput(
        text="重复文本", primary_transcript="重复文本", finalized=True, turn_id="turn_b",
    ))
    user_messages = [message for message in messages if message.get("role") == "user"]
    assert user_messages == [
        {"role": "user", "content": '用户语音最终文本：{"asr_final":"重复文本"}',
         "context_lane": ContextLane.HISTORY},
        {"role": "user", "content": "重复文本", "context_lane": ContextLane.REQUEST_TAIL,
         "context_kind": "current_user_input"},
    ]
