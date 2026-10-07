from voice_terminal.config import AppConfig
from voice_terminal.core.events import event
from voice_terminal.server.snapshot import ProtocolSequencer
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.conversation_log import USER_TURN


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


def test_speech_started_is_a_public_protocol_event():
    sequencer = ProtocolSequencer("conversation_a")
    value = event("speech.started", "input_a", speech_id="speech_a", status="playing")
    assert sequencer.public(value)
    assert sequencer.envelope(value)["type"] == "speech.started"


def test_the_ordered_conversation_is_published_on_the_public_protocol():
    sequencer = ProtocolSequencer("conversation_a")
    appended = event("conversation.append", "input_a", seq=2, kind="user_turn",
                     conversation_id="conversation_a", text="你好")
    snapshot = event("conversation.snapshot", None, entries=[], latest_seq=0)

    assert sequencer.public(appended)
    assert sequencer.public(snapshot)
    envelope = sequencer.envelope(appended)
    assert envelope["type"] == "conversation.append"
    assert envelope["payload"]["seq"] == 2
    # The envelope identity restores the turn id for clients that route on it.
    assert envelope["payload"]["turn_id"] == "input_a"


def test_subscribe_snapshot_carries_the_ordered_conversation(tmp_path):
    """A reload restores the whole rail from one server-ordered projection."""

    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / ".local")
    runtime.history.conversation.append(
        USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="灵月是什么游戏？",
    )

    payload = runtime.snapshot().payload

    assert payload["conversation"] == {
        "entries": [dict(runtime.history.conversation.entries[0])],
        "latest_seq": 1,
    }
    # The legacy projections stay published for existing consumers.
    assert payload["recent_inputs"] == []
