from voice_terminal.core.events import event
from voice_terminal.server.snapshot import ProtocolSequencer


def test_v5_envelope_has_conversation_and_resource_identities():
    sequencer = ProtocolSequencer("conversation_a")
    payload = sequencer.envelope(event("turn.started", "input_a", episode_id="episode_a", agent_step_id="decision_a"))
    assert payload == {
        "type": "input.started", "seq": 1, "timestamp": payload["timestamp"],
        "conversation_id": "conversation_a", "turn_id": "input_a", "speech_id": None,
        "payload": {"episode_id": "episode_a", "agent_step_id": "decision_a", "turn_id": "input_a"},
    }


def test_legacy_internal_names_do_not_escape_v1_projection():
    sequencer = ProtocolSequencer("session_a")
    assert not sequencer.public(event("assistant.delta", "input_a", text_delta="no"))
    assert sequencer.envelope(event("llm.context", "input_a", messages=[]))["type"] == "context.snapshot"
    assert sequencer.envelope(event("latency.snapshot", "input_a", complete=False, values={}))["type"] == "metrics.snapshot"
