from __future__ import annotations

import json

from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.shadow_agent.contracts import ShadowEvent


def information(event_id: str, text: str, *, source: str = "agent") -> ShadowEvent:
    return ShadowEvent(
        event_id=event_id,
        kind="information",
        text=text,
        based_on_context_revision=7,
        activation_id="activation-1",
        source=source,  # type: ignore[arg-type]
    )


def test_latest_information_is_a_single_global_slot_and_never_history() -> None:
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True)
    ledger.commit_input("turn-1", "开始长任务")

    assert ledger.commit_shadow_event(information("a", "A"))
    assert ledger.commit_shadow_event(information("b", "B", source="observer"))
    assert ledger.commit_shadow_event(information("c", "C"))

    snapshot = ledger.shadow_information_snapshot()
    assert snapshot is not None
    assert snapshot["text"] == "C"
    assert snapshot["source"] == "agent"
    assert all(entry.kind != "shadow_event" for entry in ledger.entries)
    history = ledger.context_messages("")
    assert "A" not in json.dumps(history, ensure_ascii=False)
    assert "B" not in json.dumps(history, ensure_ascii=False)
    assert "C" not in json.dumps(history, ensure_ascii=False)

    runtime_tail = LLMContextProjector().runtime_tail(
        current=CurrentInput(text="", finalized=False, user_speaking=False),
        speak_state={"state": "empty", "owner_scope": "none", "continuation_mode": "none"},
        shadow=ledger.shadow_runtime_tail(information=snapshot),
    )
    assert json.loads(runtime_tail.removeprefix("当前轮运行状态："))["shadow"]["information"] == {
        "text": "C", "source": "agent",
    }


def test_versioned_claim_does_not_clear_information_arriving_during_request() -> None:
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True)
    ledger.commit_shadow_event(information("c", "C"))
    claimed = ledger.shadow_information_snapshot()
    assert claimed is not None

    ledger.commit_shadow_event(information("d", "D", source="observer"))
    assert ledger.consume_shadow_information(claimed["version"]) is False
    current = ledger.shadow_information_snapshot()
    assert current is not None
    assert current["text"] == "D"
    assert current["source"] == "observer"
    assert ledger.consume_shadow_information(current["version"]) is True
    assert ledger.shadow_information_snapshot() is None


def test_terminal_event_clears_only_unclaimed_shared_information() -> None:
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True)
    ledger.commit_shadow_event(information("pending", "not yet claimed"))
    ledger.commit_shadow_event(ShadowEvent(
        event_id="result",
        kind="result",
        text="self-contained final result",
        based_on_context_revision=7,
        activation_id="activation-1",
    ))

    assert ledger.shadow_information_snapshot() is None
    shadow_entries = [entry for entry in ledger.entries if entry.kind == "shadow_event"]
    assert len(shadow_entries) == 1
    assert shadow_entries[0].content["kind"] == "result"
    assert shadow_entries[0].content["text"] == "self-contained final result"


def test_no_action_clears_slot_without_writing_semantic_history() -> None:
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True)
    ledger.commit_shadow_event(information("pending", "ephemeral"))
    ledger.commit_shadow_event(ShadowEvent(
        event_id="no-action",
        kind="no_action",
        text="nothing remains",
        based_on_context_revision=7,
        activation_id="activation-1",
    ))

    assert ledger.shadow_information_snapshot() is None
    assert all(entry.kind != "shadow_event" for entry in ledger.entries)
