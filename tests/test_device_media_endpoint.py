"""SSH 回环媒体 WebSocket 的签名握手、时钟映射和 PCM 入队回归。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.devices.capture_router import SelectableCapturePort
from voice_terminal.devices.client_tts import RemoteClientTtsSession
from voice_terminal.devices.media_protocol import (
    ClientTtsAudioFrame, MediaFrame, encode_client_tts_audio_frame, encode_frame,
)
from voice_terminal.devices.opus_codec import OpusDecoder, OpusEncoder, find_opus_library
from voice_terminal.devices.remote_playback import decode_playback_frame
from voice_terminal.devices.trust import device_request_proof_message
from voice_terminal.model_registry.service import ModelRegistryService
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.http_api import _device_media_playback_acceptance
from voice_terminal.server.websocket import SessionRuntime, create_app


class _Vault:
    """_Vault（产品 API 测试使用的空凭据库）。"""

    @property
    def available(self) -> bool:
        return True

    @property
    def diagnostic(self) -> str | None:
        return None


def test_media_endpoint_releases_cancelled_frame_without_claiming_playback() -> None:
    lease = SimpleNamespace(
        device_id="client-media", connection_generation=2,
        executor_generation=3, interaction_generation=4,
    )
    message = {"text": json.dumps({
        "type": "playback.cancelled", "protocol_version": 1,
        "device_id": "client-media", "delivery_device_id": "client-media",
        "core_session_id": "core-media", "connection_generation": 2,
        "executor_generation": 3, "interaction_generation": 4,
        "stream_id": "stream-media", "sequence": 7,
        "fields": {"sequence": "停止后丢弃的帧序号"},
    })}
    assert _device_media_playback_acceptance(
        message, lease=lease, core_session_id="core-media", stream_id="stream-media",
    ) == (7, True)

    def get(self, _reference: str) -> str | None:
        return None

    def put(self, _reference: str, _secret: str) -> None:
        return None

    def delete(self, _reference: str) -> None:
        return None


class _LocalCapture:
    """_LocalCapture（最小本机采集替身）：仅验证切换停启顺序。"""

    def __init__(self) -> None:
        self.running = False  # running（本机采集端口状态）。
        self.diagnostics: dict = {}  # diagnostics（本机采集诊断快照）。

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def read_frame(self):
        await asyncio.Future()

    def pending_frame_count(self) -> int:
        return 0

    def read_pending_frame(self):
        raise asyncio.QueueEmpty

    def preroll(self, _milliseconds: int) -> bytes:
        return b""


def _ssh_public_key(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    algorithm = b"ssh-ed25519"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", len(raw)) + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} media-test"


async def _signed_gateway_post(app, private_key, device_id: str, path: str, payload: dict):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18765") as client:
        challenge_response = await client.post("/v1/challenge", json={
            "protocol_version": 1, "device_id": device_id, "method": "POST", "path": path,
        })
        challenge = challenge_response.json()
        timestamp_ms = time.time_ns() // 1_000_000
        proof = device_request_proof_message(
            method="POST", path=path, device_id=device_id,
            core_session_id=challenge["core_session_id"], challenge=challenge["challenge"],
            timestamp_ms=timestamp_ms, payload=payload,
        )
        headers = {
            "x-voice-terminal-device-id": device_id,
            "x-voice-terminal-core-session-id": challenge["core_session_id"],
            "x-voice-terminal-challenge": challenge["challenge"],
            "x-voice-terminal-timestamp-ms": str(timestamp_ms),
            "x-voice-terminal-signature": base64.b64encode(private_key.sign(proof)).decode("ascii"),
        }
        response = await client.post(path, json=payload, headers=headers)
        return response


@pytest.mark.parametrize("downlink_codec", ["pcm", "opus"])
def test_signed_media_websocket_maps_device_clock_and_queues_tts_audio(
    tmp_path: Path, caplog, monkeypatch, downlink_codec: str,
) -> None:
    async def scenario() -> None:
        from voice_terminal.server import http_api as server_http_api

        caplog.set_level(logging.INFO, logger="voice_terminal.server.http_api")
        host_clock = [100.0]

        def monotonic_clock() -> float:
            host_clock[0] += 0.01
            return host_clock[0]

        monkeypatch.setattr(
            server_http_api, "time",
            SimpleNamespace(monotonic=monotonic_clock, time=time.time),
        )
        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "config" / "persona"),
        )
        settings = ProductSettingsStore(path=tmp_path / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / ".local")
        async def keep_test_tts_state() -> bool:
            return True
        runtime.refresh_client_tts_service = keep_test_tts_state
        model_registry = ModelRegistryService(
            config=config, registry_path=tmp_path / "config" / "model-registry.json", vault=_Vault(),
        )
        with patch.dict(os.environ, {"VOICE_WEB_ORIGINS": "http://127.0.0.1:9123"}):
            app = create_app(runtime, model_registry=model_registry, product_settings=settings)

        accepted_pcm: list[bytes] = []
        playback_progress: list[dict] = []
        runtime.audio_meter = type("Meter", (), {
            "feed": lambda _self, _direction, pcm, _rate, _channels: accepted_pcm.append(pcm),
        })()
        capture_route = SelectableCapturePort(_LocalCapture())
        runtime._capture_router = capture_route
        await capture_route.start()

        class _ListeningController:
            async def stop_listening(self) -> None:
                runtime.state.listening = False
                await capture_route.stop()

            async def start_listening(self) -> None:
                await capture_route.start()
                runtime.state.listening = True

        runtime.controller = _ListeningController()
        runtime.state.listening = True
        record_playback_progress = runtime.record_remote_playback_progress

        async def record_progress(lease, stream_id: str, receipt: dict) -> bool:
            playback_progress.append(dict(receipt))
            return await record_playback_progress(lease, stream_id, receipt)

        runtime.record_remote_playback_progress = record_progress

        player = PcmPlayer(sample_rate=16_000, channels=1, output_device=None)
        player.configure_remote_output(
            enqueue=runtime._enqueue_player_audio,
            is_bound=runtime.remote_playback_is_bound,
            progress=runtime.remote_playback_snapshot,
            buffered_ms=runtime.remote_playback_buffered_ms,
            wait_until_complete=runtime.wait_remote_playback_output,
            finish_stream=runtime.finish_remote_playback,
            transactions=runtime.remote_playback_transactions,
        )
        runtime.player = player
        player.allow("speech-media-1")

        private_key = Ed25519PrivateKey.generate()
        runtime.device_trust_store.approve("client-media", _ssh_public_key(private_key))
        connection = await runtime.open_remote_device_session("client-media", ["audio_stream", "client_tts"])
        expected_generation = runtime.device_registry.generation
        stale = await _signed_gateway_post(app, private_key, "client-media", "/v1/interaction/prepare", {
            "protocol_version": 1,
            "expected_interaction_generation": expected_generation - 1,
        })
        assert stale.status_code == 409
        assert runtime.device_registry.pending is None
        prepare = await _signed_gateway_post(app, private_key, "client-media", "/v1/interaction/prepare", {
            "protocol_version": 1,
            "expected_interaction_generation": expected_generation,
        })
        assert prepare.status_code == 200
        prepared = prepare.json()
        assert prepared["result"] == "pending_media"
        lease_generation = prepared["interaction_generation"]

        downlink_pre_skip = 0
        if downlink_codec == "opus":
            library = find_opus_library()
            if library is None:
                pytest.skip("固定 libopus 运行库未准备")
            with OpusEncoder(48_000, 64_000, library_path=library) as encoder:
                downlink_pre_skip = encoder.lookahead_samples

        handshake = {
            "protocol_version": 1,
            "stream_id": "stream-media-1",
            "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation,
            "interaction_generation": lease_generation,
            "uplink_codec": "pcm",
            "downlink_codec": downlink_codec,
            "encoder_pre_skip_samples": 0,
            "downlink_encoder_pre_skip_samples": downlink_pre_skip,
        }
        challenge_transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=challenge_transport,
                                     base_url="http://127.0.0.1:18765") as client:
            challenge_response = await client.post("/v1/challenge", json={
                "protocol_version": 1,
                "device_id": "client-media",
                "method": "GET",
                "path": "/v1/media",
            })
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()
        timestamp_ms = time.time_ns() // 1_000_000
        proof = device_request_proof_message(
            method="GET", path="/v1/media", device_id="client-media",
            core_session_id=challenge["core_session_id"], challenge=challenge["challenge"],
            timestamp_ms=timestamp_ms, payload=handshake,
        )
        headers = [
            (b"host", b"127.0.0.1:18765"),
            (b"x-voice-terminal-device-id", b"client-media"),
            (b"x-voice-terminal-core-session-id", challenge["core_session_id"].encode("ascii")),
            (b"x-voice-terminal-challenge", challenge["challenge"].encode("ascii")),
            (b"x-voice-terminal-timestamp-ms", str(timestamp_ms).encode("ascii")),
            (b"x-voice-terminal-signature", base64.b64encode(private_key.sign(proof))),
        ]
        query = (
            "protocol_version=1&stream_id=stream-media-1"
            f"&connection_generation={connection.connection_generation}"
            f"&executor_generation={connection.executor_generation}"
            f"&interaction_generation={lease_generation}"
            f"&uplink_codec=pcm&downlink_codec={downlink_codec}&encoder_pre_skip_samples=0"
            f"&downlink_encoder_pre_skip_samples={downlink_pre_skip}"
        ).encode("ascii")
        frame_pcm = b"\x01\x00" * 320
        downlink_pcm = b"\x03\x00" * 320
        frame = MediaFrame(
            stream_id="stream-media-1", sequence=0, start_sample=0, sample_count=320,
            device_time_us=1_060_000, payload=frame_pcm, device_id="client-media",
            core_session_id=runtime.core_session_id,
            connection_generation=connection.connection_generation,
            executor_generation=connection.executor_generation,
            interaction_generation=lease_generation,
        )
        incoming: asyncio.Queue[dict] = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        sent: list[dict] = []
        sent_binary: list[bytes] = []
        acknowledged_samples = 0
        receipt_sequence = 0
        playback_task: asyncio.Task | None = None

        async def receive():
            return await incoming.get()

        async def send(message: dict) -> None:
            nonlocal acknowledged_samples, receipt_sequence
            sent.append(message)
            if message.get("type") == "websocket.send" and isinstance(message.get("bytes"), bytes):
                sent_binary.append(message["bytes"])
                playback_frame = decode_playback_frame(message["bytes"])
                acknowledged_samples += playback_frame.sample_count
                receipt_sequence += 1
                enqueued_ms = acknowledged_samples * 1_000 // playback_frame.sample_rate
                await incoming.put({"type": "websocket.receive", "text": json.dumps({
                    "type": "playback.accepted", "protocol_version": 1,
                    "device_id": "client-media", "delivery_device_id": "client-media",
                    "core_session_id": runtime.core_session_id,
                    "connection_generation": connection.connection_generation,
                    "executor_generation": connection.executor_generation,
                    "interaction_generation": lease_generation,
                    "stream_id": "stream-media-1", "sequence": playback_frame.sequence,
                    "fields": {"sequence": "已加入本机播放队列的帧序号"},
                })})
                await incoming.put({"type": "websocket.receive", "text": json.dumps({
                    "type": "playback.progress", "protocol_version": 1,
                    "device_id": "client-media", "delivery_device_id": "client-media",
                    "core_session_id": runtime.core_session_id,
                    "connection_generation": connection.connection_generation,
                    "executor_generation": connection.executor_generation,
                    "interaction_generation": lease_generation,
                    "stream_id": "stream-media-1", "playback_key": playback_frame.playback_key,
                    "receipt_sequence": receipt_sequence, "sequence": playback_frame.sequence,
                    "enqueued_ms": enqueued_ms, "played_ms": enqueued_ms, "buffered_ms": 0,
                    "output_estimated_ms": enqueued_ms, "started": True,
                    "device_time_us": 1_100_000 + receipt_sequence * 20_000, "precision": "device_time",
                    "fields": {"played_ms": "设备已提交输出的时长"},
                })})
                return
            if message.get("type") != "websocket.send":
                return
            text = message.get("text")
            if not isinstance(text, str):
                return
            body = json.loads(text)
            if body.get("type") == "clock.sync.request":
                sample_index = sum(
                    1 for item in sent if item.get("type") == "websocket.send"
                    and isinstance(item.get("text"), str)
                    and json.loads(item["text"]).get("type") == "clock.sync.request"
                )
                device_receive_us = 1_000_000 + sample_index * 10_000
                await incoming.put({"type": "websocket.receive", "text": json.dumps({
                    "type": "clock.sync.response", "protocol_version": 1,
                    "sync_id": body["sync_id"],
                    "device_receive_us": device_receive_us,
                    "device_send_us": device_receive_us,
                })})
                await asyncio.sleep(0)
            elif body.get("type") == "media.ready":
                nonlocal playback_task
                active = runtime.active_remote_client_tts_outbox()
                assert active is not None
                lease, tts_outbox = active
                session = RemoteClientTtsSession(
                    runtime=runtime, lease=lease, outbox=tts_outbox,
                    playback_key="speech-media-1", model="speech-2.8-hd", voice_id="voice-a",
                    sample_rate=16_000, emotion="calm",
                )
                await session.start()

                async def relay_to_player() -> None:
                    async for pcm in session.audio_chunks():
                        await player.enqueue_async("speech-media-1", pcm)
                    await player.finish_stream_async("speech-media-1")
                    await player.wait_until_output_complete(timeout_seconds=1.0)
                    await session.close()
                    await incoming.put({"type": "websocket.disconnect", "code": 1000})

                async def drive_client_tts() -> None:
                    relay_task = asyncio.create_task(relay_to_player())
                    await session.send_text("你好。")
                    await session.flush()
                    await relay_task

                playback_task = asyncio.create_task(drive_client_tts())
                await incoming.put({"type": "websocket.receive", "bytes": encode_frame(frame)})
            elif body.get("type") == "media.tts.request":
                lease = runtime.device_registry.active
                assert lease is not None
                request_id = body["request_id"]
                task_id = body["task_id"]

                def client_status(kind: str, **extra) -> dict:
                    value = {
                        "type": kind, "protocol_version": 1,
                        "device_id": "client-media", "core_session_id": runtime.core_session_id,
                        "connection_generation": connection.connection_generation,
                        "executor_generation": connection.executor_generation,
                        "interaction_generation": lease_generation,
                        "stream_id": "stream-media-1", "task_id": task_id,
                        "request_id": request_id, **extra,
                    }
                    value["fields"] = {key: "设备媒体协议字段说明"
                                       for key in value if key != "fields"}
                    return value

                tts_pcm = b"\x03\x00" * 320
                tts_frame = ClientTtsAudioFrame(
                    device_id="client-media", core_session_id=runtime.core_session_id,
                    connection_generation=connection.connection_generation,
                    executor_generation=connection.executor_generation,
                    interaction_generation=lease_generation, stream_id="stream-media-1",
                    task_id=task_id, request_id=request_id, sequence=0, start_sample=0,
                    sample_count=320, sample_rate=16_000, device_time_us=2_000_000,
                    payload=tts_pcm,
                )
                await incoming.put({"type": "websocket.receive", "text": json.dumps(
                    client_status("media.tts.started"),
                )})
                await incoming.put({"type": "websocket.receive", "bytes": encode_client_tts_audio_frame(tts_frame)})
                await incoming.put({"type": "websocket.receive", "text": json.dumps(
                    client_status("media.tts.segment_finished", text_chars=3, end_sample=320),
                )})
                await incoming.put({"type": "websocket.receive", "text": json.dumps(
                    client_status("media.tts.completed", total_samples=320),
                )})

        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "ws", "path": "/v1/media", "raw_path": b"/v1/media",
            "query_string": query, "headers": headers, "server": ("127.0.0.1", 18_765),
            "client": ("127.0.0.1", 49152), "subprotocols": [], "state": {},
        }
        await app(scope, receive, send)
        assert playback_task is not None, (sent, caplog.text)
        await playback_task

        sent_json = [json.loads(item["text"]) for item in sent
                     if item.get("type") == "websocket.send" and isinstance(item.get("text"), str)]
        assert sum(item.get("type") == "clock.sync.request" for item in sent_json) == 5
        ready = next((item for item in sent_json if item.get("type") == "media.ready"), None)
        assert ready is not None, sent
        assert ready["capture_active"] is True
        assert ready["interaction_generation"] == lease_generation
        assert ready["encoder_pre_skip_samples"] == 0
        assert ready["downlink_codec"] == downlink_codec
        assert ready["client_tts_enabled"] is True
        assert ready["downlink_encoder_pre_skip_samples"] == downlink_pre_skip
        assert accepted_pcm == [frame_pcm]
        playback_frames = [decode_playback_frame(encoded) for encoded in sent_binary]
        assert len(playback_frames) == (1 if downlink_codec == "pcm" else 2)
        expected_output_samples = 320 if downlink_codec == "pcm" else 960
        assert sum(frame.sample_count for frame in playback_frames) == expected_output_samples
        assert [frame.start_sample for frame in playback_frames] == [
            sum(item.sample_count for item in playback_frames[:index]) for index in range(len(playback_frames))
        ]
        assert all(frame.playback_key == "speech-media-1" for frame in playback_frames)
        assert all(frame.delivery_device_id == "client-media" for frame in playback_frames)
        assert all(frame.interaction_generation == lease_generation for frame in playback_frames)
        assert all(frame.audio_codec == downlink_codec for frame in playback_frames)
        if downlink_codec == "pcm":
            assert playback_frames[0].payload == downlink_pcm
        else:
            assert all(frame.sample_rate == 48_000 for frame in playback_frames)
            with OpusDecoder(48_000, library_path=find_opus_library()) as decoder:
                decoded_samples = sum(len(decoder.decode(frame.payload)) // 2 for frame in playback_frames)
            assert decoded_samples - downlink_pre_skip >= sum(frame.sample_count for frame in playback_frames)
        assert len(playback_progress) == len(playback_frames)
        assert all(item["playback_key"] == "speech-media-1" for item in playback_progress)
        assert playback_progress[-1]["enqueued_ms"] == 20
        assert playback_progress[-1]["played_ms"] == 20
        assert playback_progress[-1]["buffered_ms"] == 0
        assert player.progress("speech-media-1")["output_estimated_ms"] == 20
        assert player.has_started("speech-media-1")
        assert playback_progress[0]["host_output_time"] > 0
        assert playback_progress[0]["precision"] == "device_time"
        assert runtime._remote_playback_outboxes == {}
        assert runtime._remote_client_tts_outboxes == {}
        assert capture_route.active_device_id is None
        assert runtime.device_registry.active is not None
        assert runtime.device_registry.active.device_id == "client-media"
        finished = await _signed_gateway_post(app, private_key, "client-media", "/v1/interaction/abort", {
            "protocol_version": 1,
            "interaction_generation": lease_generation,
        })
        assert finished.status_code == 200
        assert finished.json()["result"] == "already_finished"
        assert runtime.device_registry.active.device_id == "client-media"
        await runtime.device_remote_sessions.close_all()

    asyncio.run(scenario())


def test_unfinished_interaction_switch_expires_without_restoring_old_lease(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        import voice_terminal.server.websocket as server_websocket

        monkeypatch.setattr(server_websocket, "INTERACTION_SWITCH_TIMEOUT_SECONDS", 0.01)
        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "config" / "persona"),
        )
        settings = ProductSettingsStore(path=tmp_path / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / ".local")
        runtime._capture_router = SelectableCapturePort(_LocalCapture())
        await runtime._capture_router.start()
        await runtime.open_remote_device_session("client-expiry", ["audio_stream"])

        lease = await runtime.prepare_interaction_switch(
            "client-expiry", runtime.device_registry.generation,
        )
        assert runtime.device_registry.pending == lease
        await asyncio.sleep(0.04)
        assert runtime.device_registry.pending is None
        assert runtime.device_registry.active is None
        await runtime.device_remote_sessions.close_all()

    asyncio.run(scenario())


def test_runtime_switches_between_remote_devices_without_reusing_the_old_stream(tmp_path: Path) -> None:
    """跨远端设备切换时先撤销旧流，只授予一个新租约，并拒绝并发切换。"""
    async def scenario() -> None:
        from voice_terminal.devices.pcm_stream import PcmInputStream
        from voice_terminal.devices.remote_capture import RemoteAudioCapture, RemoteAudioCaptureError

        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "config" / "persona"),
        )
        settings = ProductSettingsStore(path=tmp_path / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / ".local")
        capture_route = SelectableCapturePort(_LocalCapture())
        runtime._capture_router = capture_route
        await capture_route.start()

        class _ListeningController:
            async def stop_listening(self) -> None:
                runtime.state.listening = False
                await capture_route.stop()

            async def start_listening(self) -> None:
                await capture_route.start()
                runtime.state.listening = True

        runtime.controller = _ListeningController()
        runtime.state.listening = True
        for device_id in ("client-a", "client-b", "client-c"):
            await runtime.open_remote_device_session(device_id, ["audio_stream"])

        def make_capture(device_id: str, lease, stream_id: str) -> RemoteAudioCapture:
            return RemoteAudioCapture(PcmInputStream(
                stream_id=stream_id,
                core_session_id=runtime.core_session_id,
                lease=lease,
                registry=runtime.device_registry,
                map_device_time=lambda value: (30.0 + value / 1_000_000, 0.001),
            ))

        def frame(device_id: str, lease, stream_id: str) -> MediaFrame:
            return MediaFrame(
                stream_id=stream_id, sequence=0, start_sample=0, sample_count=320,
                device_time_us=20_000, payload=b"\x01\x00" * 320,
                device_id=device_id, core_session_id=runtime.core_session_id,
                connection_generation=lease.connection_generation,
                executor_generation=lease.executor_generation,
                interaction_generation=lease.interaction_generation,
            )

        lease_a = await runtime.prepare_interaction_switch(
            "client-a", runtime.device_registry.generation,
        )
        capture_a = make_capture("client-a", lease_a, "stream-a")
        await runtime.select_remote_capture(capture_a, lease_a)
        await capture_a.start()
        await runtime.complete_remote_interaction_switch(capture_a, lease_a)
        assert runtime.device_registry.active == lease_a
        capture_a.accept_frame(frame("client-a", lease_a, "stream-a"))
        assert capture_a.pending_frame_count() == 1

        lease_b = await runtime.prepare_interaction_switch(
            "client-b", runtime.device_registry.generation,
        )
        assert runtime.device_registry.active is None
        assert runtime.device_registry.pending == lease_b
        assert capture_route.active_device_id is None
        assert capture_a.running is False
        with pytest.raises(RuntimeError, match="尚未完成"):
            await runtime.prepare_interaction_switch("client-c", runtime.device_registry.generation)
        assert runtime.device_registry.pending == lease_b

        capture_b = make_capture("client-b", lease_b, "stream-b")
        await runtime.select_remote_capture(capture_b, lease_b)
        await capture_b.start()
        await runtime.complete_remote_interaction_switch(capture_b, lease_b)
        assert runtime.device_registry.active == lease_b
        assert runtime.device_registry.pending is None
        assert capture_route.active_device_id == "client-b"
        assert runtime.device_registry.accepts(lease_a) is False
        with pytest.raises(RemoteAudioCaptureError, match="已停止"):
            capture_a.accept_frame(frame("client-a", lease_a, "stream-a"))
        accepted_b = capture_b.accept_frame(frame("client-b", lease_b, "stream-b"))
        assert accepted_b.start_sample == 0
        assert capture_route.read_pending_frame().captured_at == accepted_b.captured_at

        await capture_b.stop()
        await capture_route.stop()
        await runtime.device_remote_sessions.close_all()

    asyncio.run(scenario())
