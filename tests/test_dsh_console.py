"""原生网页转发的协议、流式正文、来源隔离及失效回收。"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import hashlib
import json
from types import SimpleNamespace

import h11
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from voice_terminal.dsh.console import DshConsoleProxy, DshConsoleTarget, CONSOLE_CHUNK_BYTES, browser_origin
from voice_terminal.devices.trust import DeviceRequestAuthenticator, DeviceTrustStore, device_request_proof_message
from voice_terminal.server.http_api import create_app


class BrowserTunnel:
    """BrowserTunnel（测试二进制载体）：模拟外层消息，内层使用真实 HTTP 字节。"""
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = False
        self.closed = asyncio.Event()

    async def receive_bytes(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        assert 0 < len(data) <= CONSOLE_CHUNK_BYTES
        await self.outgoing.put(data)

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed.set()
        await self.outgoing.put(None)

    async def feed(self, data):
        for offset in range(0, len(data), 31_337):
            await self.incoming.put(data[offset:offset + 31_337])


@asynccontextmanager
async def dsh_fixture():
    """隔离的 DSH 协议替身：登录、插件组合查询、二进制附件和升级字节流。"""
    requests = []
    tasks = set()
    payload = bytes(range(256)) * 3073

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        connection = h11.Connection(h11.SERVER)
        request = None
        body = bytearray()
        try:
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = await reader.read(65_536)
                    connection.receive_data(data)
                    continue
                if isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
                else:
                    return
            assert request is not None
            headers = dict(request.headers)
            requests.append((request.method, request.target, headers, bytes(body)))
            if request.target == b"/?token=fixture-only-secret":
                cookie = hashlib.sha256(headers[b"host"]).hexdigest().encode()
                writer.write(connection.send(h11.Response(status_code=303, headers=[
                    (b"location", b"./"), (b"set-cookie", b"dsh-auth-fixture=" + cookie + b"; HttpOnly; Path=/")
                ])))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                return
            expected = b"dsh-auth-fixture=" + hashlib.sha256(headers[b"host"]).hexdigest().encode()
            assert expected in headers.get(b"cookie", b"")
            if headers.get(b"upgrade") == b"websocket":
                accept = base64.b64encode(hashlib.sha1(headers[b"sec-websocket-key"] + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                writer.write(connection.send(h11.InformationalResponse(status_code=101, headers=[
                    (b"connection", b"Upgrade"), (b"upgrade", b"websocket"), (b"sec-websocket-accept", accept)])))
                await writer.drain()
                while data := await reader.read(65_536):
                    writer.write(data)
                    await writer.drain()
                return
            response = bytes(body) if request.method == b"POST" else payload
            writer.write(connection.send(h11.Response(status_code=200, headers=[
                (b"content-type", b"application/octet-stream"), (b"content-length", str(len(response)).encode()),
                (b"set-cookie", b"dsh-auth-never-expose=secret; HttpOnly"),
                (b"set-cookie", b"plugin-preference=compact; Path=/"),
                (b"content-disposition", b'attachment; filename="test.bin"'),
            ])))
            for offset in range(0, len(response), 7919):
                writer.write(connection.send(h11.Data(data=response[offset:offset + 7919])))
                await writer.drain()
                await asyncio.sleep(0)
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    target = DshConsoleTarget(origin, "fixture-generation-1", origin + "/?token=fixture-only-secret")
    try:
        yield SimpleNamespace(target=target, requests=requests, payload=payload)
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def exchange(proxy, session, *, target=b"/plugins/??one/client.js,two/client.js&rev=abc", method=b"GET", body=b"", extra=()):
    tunnel = BrowserTunnel()
    client = h11.Connection(h11.CLIENT)
    headers = [(b"host", session.authority.encode()), (b"origin", session.browser_origin.encode()),
               (b"connection", b"close"), *extra]
    if body:
        headers.append((b"content-length", str(len(body)).encode()))
    request = client.send(h11.Request(method=method, target=target, headers=headers))
    if body:
        request += client.send(h11.Data(data=body))
    request += client.send(h11.EndOfMessage())
    await tunnel.feed(request)
    task = asyncio.create_task(proxy.tunnel(tunnel, session))
    content = bytearray()
    response = None
    try:
        async with asyncio.timeout(5):
            while True:
                event = client.next_event()
                if event is h11.NEED_DATA:
                    data = await tunnel.outgoing.get()
                    assert data is not None, "代理提前结束了网页响应"
                    client.receive_data(data)
                elif isinstance(event, h11.Response):
                    response = event
                elif isinstance(event, h11.Data):
                    content.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
                else:
                    raise AssertionError(event)
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return response, bytes(content)


def test_one_signed_carrier_reuses_http_cycles_and_checks_each_request_origin():
    async def scenario():
        async with dsh_fixture() as fixture:
            proxy = DshConsoleProxy(lambda: fixture.target)
            session = await proxy.open("test-device", "test-core", "http://127.78.10.20:43210", lambda: True)
            tunnel = BrowserTunnel()
            client = h11.Connection(h11.CLIENT)
            task = asyncio.create_task(proxy.tunnel(tunnel, session))

            async def cycle(method, path, body=b""):
                headers = [(b"host", session.authority.encode()), (b"origin", session.browser_origin.encode()),
                           (b"content-length", str(len(body)).encode())]
                data = client.send(h11.Request(method=method, target=path, headers=headers))
                if body:
                    data += client.send(h11.Data(data=body))
                await tunnel.feed(data + client.send(h11.EndOfMessage()))
                output = bytearray()
                while True:
                    event = client.next_event()
                    if event is h11.NEED_DATA:
                        received = await asyncio.wait_for(tunnel.outgoing.get(), 5)
                        assert received is not None
                        client.receive_data(received)
                    elif isinstance(event, h11.Response):
                        assert event.status_code == 200
                    elif isinstance(event, h11.Data):
                        output.extend(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        client.start_next_cycle()
                        return bytes(output)
                    else:
                        raise AssertionError(event)
            try:
                assert await cycle(b"GET", b"/plugins/??one.js,two.js&rev=a") == fixture.payload
                assert await cycle(b"POST", b"/attachments", fixture.payload) == fixture.payload
                assert not task.done(), "完整响应之后应保留同一授权字节流"
                assert len([row for row in fixture.requests if row[1] == b"/attachments"]) == 1
                invalid = client.send(h11.Request(method=b"GET", target=b"/must-not-forward", headers=[
                    (b"host", session.authority.encode()), (b"origin", b"http://127.0.0.1:9")]))
                await tunnel.feed(invalid + client.send(h11.EndOfMessage()))
                await asyncio.wait_for(task, 5)
                assert all(row[1] != b"/must-not-forward" for row in fixture.requests)
                assert session.active == 0
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                proxy.close_all()
    asyncio.run(scenario())


def test_plugin_paths_binary_streams_parallel_tabs_and_upstream_credentials(caplog):
    caplog.set_level("DEBUG", logger="httpx")
    async def scenario():
        async with dsh_fixture() as fixture:
            proxy = DshConsoleProxy(lambda: fixture.target)
            session = await proxy.open("client-a", "core-a", "http://127.0.0.1:43211", lambda: True)
            results = await asyncio.gather(exchange(proxy, session), exchange(proxy, session,
                method=b"POST", target=b"/api/attachment?name=a%2Fb&x=1&x=2", body=fixture.payload,
                extra=[(b"cookie", b"voice-dsh-console-local=private; dsh-auth-forged=bad; plugin-theme=dark")]))
            for response, content in results:
                assert response.status_code == 200
                assert content == fixture.payload
                assert [value for name, value in response.headers if name == b"set-cookie"] == [b"plugin-preference=compact; Path=/"]
                assert (b"content-disposition", b'attachment; filename="test.bin"') in response.headers
            assert fixture.requests[1][1] == b"/plugins/??one/client.js,two/client.js&rev=abc"
            post = next(item for item in fixture.requests if item[0] == b"POST")
            assert post[1] == b"/api/attachment?name=a%2Fb&x=1&x=2"
            assert b"plugin-theme=dark" in post[2][b"cookie"]
            assert b"voice-dsh-console" not in post[2][b"cookie"] and b"forged" not in post[2][b"cookie"]
            assert session.active == 0
            assert "fixture-only-secret" not in repr(session) and session.cookie not in repr(session)
            assert "fixture-only-secret" not in caplog.text and session.cookie not in caplog.text
            proxy.close_all()
    asyncio.run(scenario())


def test_cross_device_origin_generation_and_revocation_close_live_upgraded_streams():
    async def scenario():
        async with dsh_fixture() as fixture:
            current = fixture.target
            authorized = True
            proxy = DshConsoleProxy(lambda: current)
            session = await proxy.open("client-a", "core-a", "http://127.0.0.1:43212", lambda: authorized)
            with pytest.raises(ValueError):
                proxy.get(session.session_id, "client-b", "core-a")
            assert proxy.valid(session)
            tunnel = BrowserTunnel()
            await tunnel.feed(b"GET /api/remote.mux HTTP/1.1\r\nHost: 127.0.0.1:43212\r\nOrigin: http://127.0.0.1:43212\r\n"
                b"Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
            task = asyncio.create_task(proxy.tunnel(tunnel, session))
            async with asyncio.timeout(5):
                first = await tunnel.outgoing.get()
                assert first.startswith(b"HTTP/1.1 101")
                payload = b"\x82\x03\x00\x01\xff"
                await tunnel.feed(payload)
                assert await tunnel.outgoing.get() == payload
                authorized = False
                await task
            assert tunnel.closed.is_set() and session.cookie == ""
            with pytest.raises(ValueError):
                proxy.get(session.session_id, "client-a", "core-a")
            authorized = True
            replacement = await proxy.open("client-a", "core-a", "http://127.0.0.1:43212", lambda: authorized)
            current = DshConsoleTarget(current.origin, "replacement-instance", current.launch_url)
            with pytest.raises(ValueError):
                proxy.get(replacement.session_id, "client-a", "core-a")
            assert replacement.cookie == ""
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["http://server:1234", "http://127.0.0.1:1234/", "http://user@127.0.0.1:1234", "http://127.0.0.1:1234?x=1"])
def test_console_origin_is_only_a_canonical_native_listener(origin):
    with pytest.raises(ValueError):
        browser_origin(origin)


def test_expiry_removes_upstream_credentials_without_waiting_for_another_request(monkeypatch):
    async def scenario():
        async with dsh_fixture() as fixture:
            monkeypatch.setattr("voice_terminal.dsh.console.CONSOLE_SESSION_SECONDS", .05)
            proxy = DshConsoleProxy(lambda: fixture.target)
            session = await proxy.open("client-a", "core-a", "http://127.0.0.1:43214", lambda: True)
            await asyncio.wait_for(session.stop.wait(), 1)
            assert session.cookie == "" and session.expiry_timer is None
            with pytest.raises(ValueError):
                proxy.get(session.session_id, "client-a", "core-a")
    asyncio.run(scenario())


def test_idle_revocation_and_missing_native_heartbeat_clear_credentials(monkeypatch):
    async def scenario():
        async with dsh_fixture() as fixture:
            allowed = True
            proxy = DshConsoleProxy(lambda: fixture.target)
            session = await proxy.open("client-a", "core-a", "http://127.76.42.19:43215", lambda: allowed)
            allowed = False
            await asyncio.wait_for(session.stop.wait(), 2)
            assert session.cookie == "" and session.active == 0
            allowed = True
            monkeypatch.setattr("voice_terminal.dsh.console.CONSOLE_LEASE_SECONDS", .05)
            session = await proxy.open("client-a", "core-a", "http://127.76.42.19:43215", lambda: allowed)
            await asyncio.wait_for(session.stop.wait(), 2)
            assert session.cookie == "" and session.active == 0
    asyncio.run(scenario())


def test_console_management_uses_real_signed_challenges_and_gateway_guard(tmp_path):
    async def scenario():
        async with dsh_fixture() as fixture:
            key = Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
            trust = DeviceTrustStore(tmp_path)
            trust.approve("client-a", public)
            core = "a" * 32
            proxy = DshConsoleProxy(lambda: fixture.target)
            runtime = SimpleNamespace(dsh=SimpleNamespace(console=proxy), core_session_id=core,
                device_trust_store=trust, device_request_authenticator=DeviceRequestAuthenticator(trust, core_session_id=core),
                _device_gateway_enabled=True, _device_gateway_port=43127)
            app = create_app(runtime)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:43127") as client:
                body = {"protocol_version": 1, "action": "open", "browser_origin": "http://127.0.0.1:43213"}
                assert (await client.post("/v1/dsh-console", json=body)).status_code == 401

                async def signed(payload):
                    challenge = (await client.post("/v1/challenge", json={"protocol_version":1,"device_id":"client-a","method":"POST","path":"/v1/dsh-console"})).json()
                    import time
                    timestamp = int(time.time() * 1000)
                    proof = device_request_proof_message(method="POST", path="/v1/dsh-console", device_id="client-a",
                        core_session_id=core, challenge=challenge["challenge"], timestamp_ms=timestamp, payload=payload)
                    headers = {"x-voice-terminal-device-id":"client-a","x-voice-terminal-core-session-id":core,
                        "x-voice-terminal-challenge":challenge["challenge"],"x-voice-terminal-timestamp-ms":str(timestamp),
                        "x-voice-terminal-signature":base64.b64encode(key.sign(proof)).decode()}
                    return await client.post("/v1/dsh-console", json=payload, headers=headers)

                opened = await signed(body)
                assert opened.status_code == 200, opened.text
                value = opened.json()
                assert value["state"] == "ready" and "cookie" not in value and "token" not in opened.text
                status = {"protocol_version":1,"action":"status","console_session_id":value["console_session_id"]}
                assert (await signed(status)).status_code == 200
                runtime._device_gateway_enabled = False
                assert (await signed(status)).status_code == 409
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as ordinary:
                    assert (await ordinary.post("/v1/dsh-console", json=body)).status_code == 404
    asyncio.run(scenario())
