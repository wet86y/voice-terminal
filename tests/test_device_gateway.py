"""Device gateway port guard（设备回环网关端口白名单）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx

from voice_terminal.devices.file_transfer import DeviceFileTransferStore
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.server.http_api import (
    LoopbackDeviceGatewayGuard, _device_gateway_challenge_payload, _device_gateway_payload,
    _device_media_open_payload, _device_media_clock_response, create_app,
)


def test_live_gateway_port_switch_preserves_whitelist_and_rejects_old_port():
    runtime = SimpleNamespace(_device_gateway_port=43127, health_payload=lambda: {"same_core": True})
    app = create_app(runtime)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:43127") as client:
            assert (await client.get("/health")).status_code == 404
            # 无凭据不通过业务鉴权，但请求已通过当前网关端口门禁。
            payload = {"protocol_version": 1, "device_id": "client", "method": "POST", "path": "/v1/status"}
            assert (await client.post("/v1/challenge", json=payload, headers={"Host": "127.0.0.1:18765"})).status_code == 503
            runtime._device_gateway_port = 43128
            assert (await client.post("/v1/challenge", json={}, headers={"Host": "127.0.0.1:43128"})).status_code == 404
            assert (await client.get("/health")).json() == {"same_core": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:43128") as client:
            assert (await client.get("/health")).status_code == 404
            runtime._device_gateway_port = None
            assert (await client.post("/v1/challenge", json={})).status_code == 404
    asyncio.run(scenario())


def _scope(port: int, *, method: str = "GET", path: str = "/health", kind: str = "http"):
    return {
        "type": kind,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("127.0.0.1", port),
        "client": ("127.0.0.1", 1),
    }


def test_gateway_listener_rejects_every_route_except_explicit_device_endpoints() -> None:
    calls: list[str] = []

    async def application(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    allowed_paths = frozenset({"/v1/challenge", "/v1/status", "/v1/devices", "/v1/session/open",
                               "/v1/session/poll", "/v1/session/complete", "/v1/session/close"})
    guarded = LoopbackDeviceGatewayGuard(application, gateway_port=18765, allowed_paths=allowed_paths)

    async def invoke(scope):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await guarded(scope, receive, send)
        return messages

    denied = asyncio.run(invoke(_scope(18765, method="GET", path="/health")))
    assert denied[0]["status"] == 404
    assert calls == []

    denied_post = asyncio.run(invoke(_scope(18765, method="POST", path="/api/v1/product/snapshot")))
    assert denied_post[0]["status"] == 404
    assert calls == []

    allowed = asyncio.run(invoke(_scope(18765, method="POST", path="/v1/status")))
    assert allowed[0]["status"] == 200
    assert calls == ["/v1/status"]

    allowed = asyncio.run(invoke(_scope(18765, method="POST", path="/v1/devices")))
    assert allowed[0]["status"] == 200
    assert calls == ["/v1/status", "/v1/devices"]

    allowed = asyncio.run(invoke(_scope(18765, method="POST", path="/v1/challenge")))
    assert allowed[0]["status"] == 200
    assert calls == ["/v1/status", "/v1/devices", "/v1/challenge"]

    for path in ("/v1/session/open", "/v1/session/poll", "/v1/session/complete", "/v1/session/close"):
        allowed = asyncio.run(invoke(_scope(18765, method="POST", path=path)))
        assert allowed[0]["status"] == 200
        assert calls[-1] == path


def test_main_application_port_keeps_existing_http_routes() -> None:
    calls: list[str] = []

    async def application(scope, receive, send):
        calls.append(scope["path"])

    guarded = LoopbackDeviceGatewayGuard(
        application, gateway_port=18765,
        allowed_paths=frozenset({"/v1/challenge", "/v1/status", "/v1/devices", "/v1/session/open",
                                 "/v1/session/poll", "/v1/session/complete", "/v1/session/close"}),
    )

    async def invoke():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await guarded(_scope(45678, path="/api/v1/product/snapshot"), receive, send)

    asyncio.run(invoke())
    assert calls == ["/api/v1/product/snapshot"]


def test_file_gateway_accepts_raw_upload_chunks_on_the_original_ticket(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        connection = registry.connect("client-a", capabilities=("file_transfer",))
        store = DeviceFileTransferStore(tmp_path)
        runtime = SimpleNamespace(device_registry=registry, device_file_transfer_store=store)
        from tests.device_gateway_support import configure_test_gateway
        port = configure_test_gateway(runtime)
        app = create_app(runtime)
        raw = bytes(range(256)) * 256
        sha256 = hashlib.sha256(raw).hexdigest()
        ticket = {
            "transfer_id": "a" * 32, "device_id": connection.device_id,
            "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation,
        }
        store.prepare_screen_upload(**ticket)
        channel = store.open_device_file_channel(**ticket, operation="file.sftp.upload", maximum_bytes=len(raw))
        identity = {**ticket, "channel_token": channel["channel_token"]}
        metadata = {"protocol_version": 1, "action": "upload_chunk", **identity,
                    "offset": 0, "chunk_sha256": sha256}
        metadata["fields"] = dict.fromkeys(metadata, "文件上传分块元数据")
        packed = json.dumps(metadata).encode("utf-8")
        framed = len(packed).to_bytes(4, "big") + packed + raw
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{port}") as client:
            headers = {"content-type": "application/octet-stream"}
            url = "/v1/file-transfer"
            response = await client.post(url, content=framed[:-1] + b"\x00", headers=headers)
            assert response.status_code == 409
            response = await client.post(url, content=framed, headers=headers)
            assert response.status_code == 200
            assert response.json()["transfer"]["next_offset"] == len(raw)
            response = await client.post(url, content=framed, headers=headers)
            assert response.status_code == 200
        store.finish_device_channel_upload(**identity, expected_bytes=len(raw), expected_sha256=sha256)
        assert store.consume_screen_upload(**ticket, expected_bytes=len(raw), expected_sha256=sha256) == raw
        assert not list(store.root.iterdir())

    asyncio.run(scenario())


def test_gateway_listener_allows_only_registered_websockets():
    calls: list[str] = []

    async def application(scope, _receive, send):
        calls.append(scope["path"])
        await send({"type": "websocket.accept", "subprotocol": None, "headers": []})

    guarded = LoopbackDeviceGatewayGuard(
        application, gateway_port=18765,
        allowed_paths=frozenset({"/v1/status", "/v1/media", "/v1/voice"}),
    )

    async def invoke(path: str):
        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await guarded(_scope(18765, path=path, kind="websocket"), receive, send)
        return messages

    # /v1/voice（认证语音控制通道）与 /v1/media（媒体通道）都必须按登记放行：
    # 远端语音转发经 SSH 固定桥接落到 18765，若此处拒绝则转发永远握手失败。
    assert asyncio.run(invoke("/v1/voice")) == [
        {"type": "websocket.accept", "subprotocol": None, "headers": []},
    ]
    assert asyncio.run(invoke("/v1/media")) == [
        {"type": "websocket.accept", "subprotocol": None, "headers": []},
    ]
    # 未在 allowed_paths（允许路径）登记的业务 WebSocket 仍必须关闭。
    assert asyncio.run(invoke("/api/v1/product/snapshot")) == [
        {"type": "websocket.close", "code": 1008},
    ]
    assert calls == ["/v1/voice", "/v1/media"]
    assert asyncio.run(invoke("/ws")) == [{"type": "websocket.close", "code": 1008}]


def test_gateway_listener_requires_explicit_registration_even_for_voice_websocket():
    calls: list[str] = []

    async def application(scope, _receive, send):
        calls.append(scope["path"])
        await send({"type": "websocket.accept", "subprotocol": None, "headers": []})

    guarded = LoopbackDeviceGatewayGuard(
        application, gateway_port=18765,
        allowed_paths=frozenset({"/v1/status", "/v1/media"}),
    )

    async def invoke(path: str):
        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await guarded(_scope(18765, path=path, kind="websocket"), receive, send)
        return messages

    assert asyncio.run(invoke("/ws")) == [{"type": "websocket.close", "code": 1008}]
    assert calls == []


def test_device_gateway_payload_requires_bounded_exact_version_envelope() -> None:
    from fastapi import HTTPException

    class Request:
        def __init__(self, payload: bytes, declared_length: str | None = None) -> None:
            self.headers = {} if declared_length is None else {"content-length": declared_length}
            self.payload = payload

        async def stream(self):
            yield self.payload

    assert asyncio.run(_device_gateway_payload(Request(b'{"protocol_version":1}'))) == {"protocol_version": 1}
    for request, status in (
        (Request(b"{"), 400),
        (Request(b'{"protocol_version":true}'), 422),
        (Request(b'{"protocol_version":1,"protocol_version":1}'), 400),
        (Request(b'{"protocol_version":1,"device_id":"secret"}'), 422),
        (Request(b'{"protocol_version":1}', "4097"), 413),
        (Request(b" " * 4097), 413),
    ):
        try:
            asyncio.run(_device_gateway_payload(request))
        except HTTPException as error:
            assert error.status_code == status
        else:
            raise AssertionError(f"expected HTTP {status} for malformed gateway payload")


def test_device_gateway_challenge_requires_exact_device_target_and_method() -> None:
    from fastapi import HTTPException

    class Request:
        headers = {}

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        async def stream(self):
            yield self.payload

    request = Request(b'{"protocol_version":1,"device_id":"client-a","method":"POST","path":"/v1/status"}')
    assert asyncio.run(_device_gateway_challenge_payload(request)) == {
        "protocol_version": 1, "device_id": "client-a", "method": "POST", "path": "/v1/status",
    }
    for payload in (
        b'{"protocol_version":1,"device_id":"client-a","method":"GET","path":"/v1/status"}',
        b'{"protocol_version":1,"device_id":"client-a","method":"POST","path":"/v1/pairing/request"}',
    ):
        try:
            values = asyncio.run(_device_gateway_challenge_payload(Request(payload)))
            assert values["method"] == "POST" and values["path"] in {
                "/v1/status", "/v1/devices", "/v1/session/open", "/v1/session/poll",
                "/v1/session/complete", "/v1/session/close",
            }
        except HTTPException as error:
            assert error.status_code == 422

    media = Request(b'{"protocol_version":1,"device_id":"client-a","method":"GET","path":"/v1/media"}')
    assert asyncio.run(_device_gateway_challenge_payload(media)) == {
        "protocol_version": 1, "device_id": "client-a", "method": "GET", "path": "/v1/media",
    }


def test_device_media_open_payload_rejects_duplicate_or_noncanonical_query_fields():
    class WebSocket:
        def __init__(self, query: bytes) -> None:
            self.scope = {"query_string": query}

    payload = _device_media_open_payload(WebSocket(
        b"protocol_version=4&stream_id=stream-a&connection_generation=2&executor_generation=3&interaction_generation=4",
    ))
    assert payload == {
        "protocol_version": 4,
        "stream_id": "stream-a",
        "connection_generation": 2,
        "executor_generation": 3,
        "interaction_generation": 4,
    }

    from fastapi import HTTPException
    for query in (
        b"protocol_version=4&protocol_version=4&stream_id=s&connection_generation=1&executor_generation=1&interaction_generation=1",
        b"protocol_version=02&stream_id=s&connection_generation=1&executor_generation=1&interaction_generation=1",
        b"protocol_version=4&stream_id=s&connection_generation=1&executor_generation=1&interaction_generation=1&unexpected=x",
    ):
        try:
            _device_media_open_payload(WebSocket(query))
        except HTTPException as error:
            assert error.status_code in {400, 422}
        else:
            raise AssertionError("expected malformed media query rejection")


def test_device_media_clock_response_requires_a_unique_exact_control_envelope():
    valid = {"text": '{"type":"clock.sync.response","protocol_version":4,"sync_id":"sync-a",'
                     '"device_receive_us":1000,"device_send_us":1100}'}
    assert _device_media_clock_response(valid, sync_id="sync-a") == (1000, 1100)
    invalid = {"text": '{"type":"clock.sync.response","protocol_version":4,"sync_id":"sync-a",'
                       '"device_receive_us":1000,"device_receive_us":1000,"device_send_us":1100}'}
    try:
        _device_media_clock_response(invalid, sync_id="sync-a")
    except ValueError:
        pass
    else:
        raise AssertionError("expected duplicate-field clock response rejection")
