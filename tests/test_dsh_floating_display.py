"""后台图文工具经正式鉴权回调进入本机悬浮窗，保留共享投递与幂等边界。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.test_dsh_bridge import FakeClient, FakeContext
from voice_terminal.agent.display_decision import DisplayDecision
from voice_terminal.config import AppConfig
from voice_terminal.dsh.bridge import DshBridgeHub, DshMappingStore, DshShadowAgentSession
from voice_terminal.server.http_api import create_app
from voice_terminal.server.websocket import SessionRuntime


@pytest.fixture
def display_runtime(tmp_path):
    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
    session = DshShadowAgentSession(
        client=FakeClient(), mapping_store=DshMappingStore(tmp_path / "mapping.json"),
        context_source=FakeContext(), voice_session_id=runtime.conversation.conversation_id, workspace=tmp_path,
    )
    session.activation_id = "background-activation"
    session.last_applied_revision = 2
    runtime.shadow_session = session
    runtime.dsh.bridge_hub = DshBridgeHub("isolated-display-token")
    runtime.dsh.bridge_hub.register(session)
    payload = {
        "protocol_version": "2.0", "request_id": "display-1", "voice_session_id": session.voice_session_id,
        "dsh_session_id": session.dsh_session_id, "epoch": session.epoch, "context_revision": 2,
        "activation_id": session.activation_id, "display": [{"content": "# 后台图文", "duration_seconds": 12}],
    }
    return runtime, session, payload


def post(client, payload, *, authorized=True):
    return client.post("/api/internal/dsh-bridge/display.publish", json=payload,
                       headers={"Authorization": "Bearer isolated-display-token"} if authorized else {})


def local_client(runtime):
    app = create_app(runtime)

    async def transport(scope, receive, send):
        # 当前受管 Starlette（测试传输）版本通过 ASGI（服务接口）请求来源模拟本机调用。
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": ("127.0.0.1", 1234)}
        await app(scope, receive, send)

    return TestClient(transport)


def read(socket, kind):
    for _ in range(12):
        packet = socket.receive_json()
        if packet["type"] == kind:
            return packet
    raise AssertionError("未收到预期事件 " + kind)


def test_background_show_clear_and_voice_decisions_share_one_delivery_sequence(display_runtime):
    runtime, session, payload = display_runtime
    with local_client(runtime) as client:
        with client.websocket_connect("/ws") as desktop:
            desktop.send_json({"type": "command", "request_id": "mode", "command": "display.mode",
                               "payload": {"enabled": True, "generation": 7}})
            desktop.send_json({"type": "command", "request_id": "subscribe", "command": "subscribe", "payload": {}})
            read(desktop, "runtime.snapshot")
            assert runtime.state.listening is False
            first = post(client, payload)
            assert first.status_code == 200
            assert first.json()["status"] == "submitted"
            assert first.json()["delivery"] == 1
            assert read(desktop, "display.command")["payload"]["content"] == "# 后台图文"
            replay = post(client, payload)
            assert replay.json()["result"] == "duplicate"
            assert replay.json()["delivery"] == 1
            assert post(client, {**payload, "display": [{"cancel": True}]}).status_code == 409
            assert client.portal is not None
            client.portal.call(runtime.floating_display.submit, runtime.floating_display.target(),
                               runtime.conversation.conversation_id, "voice-turn", DisplayDecision("语音侧补充", 5))
            assert read(desktop, "display.command")["payload"]["delivery"] == 2
            cleared = post(client, {**payload, "request_id": "clear-1", "display": [{"cancel": True}]})
            assert cleared.json()["delivery"] == 3
            cancel = read(desktop, "display.command")["payload"]
            assert cancel["content"] is None and cancel["duration_seconds"] is None
            assert runtime.state.listening is False
            assert session.activation_id == "background-activation"


def test_background_display_rejects_unavailable_mode_and_old_activation(display_runtime):
    runtime, session, payload = display_runtime
    with local_client(runtime) as client:
        assert post(client, payload, authorized=False).status_code == 401
        unavailable = post(client, payload)
        assert unavailable.status_code == 409
        assert unavailable.json()["result"] == "unavailable"
        session.activation_id = "different-activation"
        assert post(client, payload).json()["result"] == "stale"
        session.activation_id = None
        assert post(client, payload).json()["result"] == "stale"
        assert runtime.floating_display._delivery == 0


def test_background_display_does_not_adopt_a_mode_changed_during_binding_check(display_runtime):
    runtime, session, payload = display_runtime
    original = session._callback_matches_or_reconcile

    async def check_and_switch(value):
        matched = await original(value)
        target = runtime.floating_display.target()
        assert target is not None
        runtime.floating_display.set_mode(target.connection_id, {"enabled": True, "generation": 8})
        return matched

    session._callback_matches_or_reconcile = check_and_switch
    with local_client(runtime) as client:
        with client.websocket_connect("/ws") as desktop:
            desktop.send_json({"type": "command", "request_id": "mode", "command": "display.mode",
                               "payload": {"enabled": True, "generation": 7}})
            desktop.send_json({"type": "command", "request_id": "subscribe", "command": "subscribe", "payload": {}})
            read(desktop, "runtime.snapshot")
            response = post(client, payload)
            assert response.status_code == 409
            assert response.json()["result"] == "unavailable"
            assert runtime.floating_display._delivery == 0


@pytest.mark.parametrize("display", [[], [{"cancel": False}], [{"content": "正文", "duration_seconds": 4}],
                                     [{"content": "正文", "duration_seconds": True}], [{"cancel": True}, {"cancel": True}]])
def test_background_display_validates_shared_content_contract(display_runtime, display):
    runtime, _, payload = display_runtime
    with local_client(runtime) as client:
        assert post(client, {**payload, "display": display}).status_code == 422
        assert runtime.floating_display._delivery == 0


def test_concurrent_background_retries_publish_once_and_retry_unavailable_is_allowed(display_runtime):
    runtime, session, payload = display_runtime

    async def run():
        calls = []

        async def unavailable():
            return {"result": "unavailable", "reason": "悬浮窗未启用"}

        async def publish():
            calls.append(True)
            await asyncio.sleep(0)
            return {"result": "accepted", "status": "submitted", "generation": 5, "delivery": 9}

        assert (await session.accept_display(payload, unavailable))["result"] == "unavailable"
        first, second = await asyncio.gather(session.accept_display(payload, publish),
                                             session.accept_display(payload, publish))
        assert first["result"] == "accepted"
        assert second["result"] == "duplicate"
        assert first["delivery"] == second["delivery"] == 9
        assert calls == [True]
        invalid = {**payload, "request_id": "unexpected", "device_id": "remote"}
        assert (await session.accept_display(invalid, publish))["result"] == "conflict"
        assert runtime.state.listening is False

    asyncio.run(run())
