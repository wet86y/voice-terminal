"""真实媒体处理器的虚拟长时运行：续测、空闲后收音和十次输入重建。"""
import asyncio
import base64
import json
import time
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.devices.capture_router import SelectableCapturePort
from voice_terminal.devices.media_protocol import MediaFrame, encode_frame
from voice_terminal.devices.trust import device_request_proof_message
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from tests.test_device_media_endpoint import _LocalCapture, _simulate_native_trust_write, _ssh_public_key


def test_authenticated_media_survives_ten_minutes_and_ten_input_restarts(tmp_path, monkeypatch):
    async def run():
        from voice_terminal.server import http_api
        clock = [100.0]
        monkeypatch.setattr(http_api, "media_clock", lambda: clock[0])
        config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"))
        settings = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / "runtime")
        from tests.device_gateway_support import configure_test_gateway
        gateway_port = configure_test_gateway(runtime)
        route = SelectableCapturePort(_LocalCapture())
        runtime._capture_router = route

        class Controller:
            async def start_listening(self):
                await route.start()
                runtime.state.listening = True

            async def stop_listening(self):
                runtime.state.listening = False
                await route.stop()

        runtime.controller = Controller()
        await runtime.controller.start_listening()
        private_key = Ed25519PrivateKey.generate()
        device_id = "client-continuity"
        _simulate_native_trust_write(runtime, device_id, _ssh_public_key(private_key))
        connection = await runtime.open_remote_device_session(device_id, ["audio_stream"])
        lease = await runtime.prepare_interaction_switch(device_id, runtime.device_registry.generation)
        app = create_app(runtime, product_settings=settings)
        handshake = {"protocol_version": 4, "stream_id": "media-continuity",
                     "connection_generation": connection.connection_generation,
                     "executor_generation": connection.executor_generation,
                     "interaction_generation": lease.interaction_generation}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{gateway_port}") as client:
            response = await client.post("/v1/challenge", json={"protocol_version": 1, "device_id": device_id,
                                                                "method": "GET", "path": "/v1/media"})
        assert response.status_code == 200
        challenge = response.json()
        timestamp = time.time_ns() // 1_000_000
        proof = device_request_proof_message(method="GET", path="/v1/media", device_id=device_id,
            core_session_id=runtime.core_session_id, challenge=challenge["challenge"], timestamp_ms=timestamp, payload=handshake)
        headers = [(b"host", f"127.0.0.1:{gateway_port}".encode()), (b"x-voice-terminal-device-id", device_id.encode()),
                   (b"x-voice-terminal-core-session-id", runtime.core_session_id.encode()),
                   (b"x-voice-terminal-challenge", challenge["challenge"].encode()),
                   (b"x-voice-terminal-timestamp-ms", str(timestamp).encode()),
                   (b"x-voice-terminal-signature", base64.b64encode(private_key.sign(proof)))]
        incoming = asyncio.Queue()
        pong = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        measurements = []
        input_ids = []
        driver = None

        async def ping(sequence):
            await incoming.put({"type": "websocket.receive", "text": json.dumps({
                "type": "media.ping", "protocol_version": 4, "sequence": sequence})})
            assert await asyncio.wait_for(pong.get(), 2) == sequence

        async def drive():
            for step in range(61):
                clock[0] = 100.0 + step * 10
                if 1 <= step <= 10:
                    await runtime.controller.stop_listening()
                    await runtime.controller.start_listening()
                    input_ids.append(runtime._remote_media_captures[device_id].stream_id)
                await ping(step)
                await ping(1000 + step)  # 队列屏障：前一轮同步回执先于本次保活处理。
                assert runtime.device_registry.active == lease
            capture = runtime._remote_media_captures[device_id]
            frame = MediaFrame(stream_id=capture.stream_id, device_id=device_id, core_session_id=runtime.core_session_id,
                connection_generation=lease.connection_generation, executor_generation=lease.executor_generation,
                interaction_generation=lease.interaction_generation, sequence=0, start_sample=0,
                sample_count=320, device_time_us=int((clock[0] - 90) * 1_000_000), payload=b"\x01\x00" * 320)
            await incoming.put({"type": "websocket.receive", "bytes": encode_frame(frame)})
            await ping(2000)
            assert capture.diagnostics["blocks"] == 1 and capture.running
            assert len(set(input_ids)) == 10
            await incoming.put({"type": "websocket.disconnect", "code": 1000})

        async def send(message):
            nonlocal driver
            if message["type"] == "websocket.close":
                raise AssertionError(f"长时媒体被提前关闭：{message}")
            if not isinstance(message.get("text"), str):
                return
            body = json.loads(message["text"])
            if body["type"] == "clock.sync.request":
                measurements.append(clock[0])
                device_time = int((clock[0] - 90) * 1_000_000)
                await incoming.put({"type": "websocket.receive", "text": json.dumps({
                    "type": "clock.sync.response", "protocol_version": 4, "sync_id": body["sync_id"],
                    "device_receive_us": device_time, "device_send_us": device_time})})
            elif body["type"] == "media.ready":
                assert len(measurements) == 1
                driver = asyncio.create_task(drive())
            elif body["type"] == "media.pong":
                await pong.put(body["sequence"])

        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws",
                 "path": "/v1/media", "raw_path": b"/v1/media", "query_string": urlencode(handshake).encode(),
                 "headers": headers, "server": ("127.0.0.1", gateway_port), "client": ("127.0.0.1", 45123),
                 "subprotocols": [], "state": {}}
        try:
            await asyncio.wait_for(app(scope, incoming.get, send), 5)
            assert driver is not None
            await driver
            assert len(measurements) >= 61
            assert runtime.device_registry.active.device_id == "local"
        finally:
            if driver is not None:
                if not driver.done():
                    driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)
            await runtime.device_remote_sessions.close_all()

    asyncio.run(run())
