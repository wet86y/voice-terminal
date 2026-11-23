from voice_terminal.config import AppConfig
from voice_terminal.core.events import event
from voice_terminal.server.snapshot import ProtocolSequencer
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.conversation_log import USER_TURN
from unittest.mock import AsyncMock

import pytest


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


@pytest.mark.parametrize("operation", ["reset", "clear"])
@pytest.mark.parametrize("fails", [False, True])
def test_manual_session_commands_return_correlated_completion_or_failure(tmp_path, operation, fails):
    from fastapi.testclient import TestClient
    from voice_terminal.server.http_api import create_app

    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
    action = AsyncMock(side_effect=RuntimeError("archive unavailable") if fails else None)
    setattr(runtime, f"{operation}_session", action)
    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "command", "request_id": "operation-request",
                              "command": f"session.{operation}", "payload": {}})
            response = socket.receive_json()
            assert response["payload"]["request_id"] == "operation-request"
            if fails:
                assert response["type"] == "error"
                assert response["payload"]["code"] == (
                    "HANDOFF_RESET_FAILED" if operation == "reset" else "SESSION_CLEAR_FAILED")
            else:
                assert response["type"] == "session.operation"
                assert response["payload"]["operation"] == operation
                assert response["payload"]["status"] == "completed"
    action.assert_awaited_once()

def test_floating_display_delivery_and_receipts_are_connection_scoped(tmp_path):
    from fastapi.testclient import TestClient
    from voice_terminal.agent.display_decision import DisplayDecision
    from voice_terminal.server.http_api import create_app

    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
    display = runtime.floating_display

    def command(connection, name, payload):
        connection.send_json({"type": "command", "request_id": name, "command": name, "payload": payload})

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws") as observer, client.websocket_connect("/ws") as desktop:
            command(observer, "subscribe", {})
            assert observer.receive_json()["type"] == "runtime.snapshot"
            command(desktop, "display.mode", {"enabled": True, "generation": 4})
            command(desktop, "subscribe", {})
            assert desktop.receive_json()["type"] == "runtime.snapshot"
            owner = display.target()
            assert owner is not None and owner.generation == 4
            assert client.portal is not None
            conversation_id = runtime.conversation.conversation_id

            client.portal.call(display.submit, owner, conversation_id, "input_a", DisplayDecision("# 正文", 5))
            def read(connection, kind):
                for _ in range(8):
                    message = connection.receive_json()
                    if message["type"] == kind:
                        return message
                raise AssertionError(f"未收到 {kind}（预期语音事件）")

            packet = read(desktop, "display.command")
            assert packet["payload"] == {"generation": 4, "delivery": 1, "content": "# 正文",
                                         "duration_seconds": 5, "turn_id": "input_a"}
            client.portal.call(runtime.bus.publish, event("input.started", "input_b"))
            for _ in range(8):
                observer_packet = observer.receive_json()
                assert observer_packet["type"] != "display.command"
                if observer_packet["type"] == "input.started":
                    break
            else:
                raise AssertionError("观察连接未收到公共语音事件")

            receipt = {"generation": 4, "delivery": 1, "status": "visible", "reason": None}
            command(observer, "display.receipt", receipt)
            command(observer, "subscribe", {})
            read(observer, "runtime.snapshot")
            assert display.feedback == ""
            command(desktop, "display.receipt", receipt)
            command(desktop, "subscribe", {})
            read(desktop, "runtime.snapshot")
            assert display.feedback == "图文已经显示。"

            client.portal.call(display.submit, owner, conversation_id, "input_a", DisplayDecision())
            packet = read(desktop, "display.command")
            assert packet["payload"]["delivery"] == 2 and packet["payload"]["content"] is None
            command(desktop, "display.mode", {"enabled": False, "generation": 5})
            command(desktop, "subscribe", {})
            read(desktop, "runtime.snapshot")
            assert display.target() is None
            client.portal.call(display.submit, owner, conversation_id, "input_a", DisplayDecision("过期内容", 5))
            assert display.feedback == ""
