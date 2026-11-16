"""Client-aware TTS provider（按活跃设备选择客户端凭据）测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.devices.client_tts import RemoteClientTtsOutbox
from voice_terminal.devices.registry import InteractionLease
from voice_terminal.providers.client_tts import ClientAwareMiniMaxTtsClient
from voice_terminal.tts.controls import SpeechControls
from voice_terminal.tts.router import TtsBackendRouter
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime


class _HostTts:
    transport = "websocket"
    scheduling = "direct"
    effective_scheduling = "direct"

    def session(self, **kwargs):
        return kwargs

    async def close(self):
        return None


def test_router_binds_new_speech_to_client_tts_lease_and_freezes_playback_key() -> None:
    async def scenario():
        lease = InteractionLease(
            device_id="client-a", interaction_generation=4,
            connection_generation=2, executor_generation=3,
        )
        outbox = RemoteClientTtsOutbox(
            lease=lease, core_session_id="core-a", stream_id="media-a",
        )

        async def bind_remote_playback_target(playback_key, candidate):
            return playback_key == "turn-a:2" and candidate == lease

        runtime = SimpleNamespace(
            active_remote_client_tts_outbox=lambda: (lease, outbox),
            bind_remote_playback_target=bind_remote_playback_target,
        )
        config = SimpleNamespace(
            online_sample_rate=32_000, sample_rate=32_000, model="speech-2.8-hd",
            voice_id="voice-a", default_emotion="calm",
        )
        adaptive = ClientAwareMiniMaxTtsClient(runtime=runtime, fallback=_HostTts(), config=config)
        router = TtsBackendRouter(remote=adaptive, backend="minimax")
        backend, client = router.bind()
        assert backend == "minimax"
        assert client.uses_client_tts is True
        assert client.transport == "websocket_bidi"
        session = client.session(
            playback_key="turn-a:2", turn_id="turn-a", speech_id="speech-a",
            controls=SpeechControls(guidance="happy", speed=1.2, volume=0.8),
        )
        await session.start()
        sending = asyncio.create_task(session.send_text("客户端语音"))
        request = await outbox.next_control()
        assert request["type"] == "media.tts.request"
        assert request["task_id"] != "turn-a:2"
        assert request["model"] == "speech-2.8-hd"
        assert request["voice_id"] == "voice-a"
        assert request["sample_rate"] == 32_000
        assert request["speed"] == 1.2
        assert request["volume"] == 0.8
        assert request["emotion"] == "happy"
        assert "api_key" not in request
        state = outbox.request(request["request_id"])
        assert state is not None
        outbox.accept_control({
            "type": "media.tts.started", "protocol_version": 1,
            "device_id": "client-a", "core_session_id": "core-a",
            "connection_generation": 2, "executor_generation": 3,
            "interaction_generation": 4, "stream_id": "media-a",
            "task_id": state.task_id, "request_id": state.request_id,
            "fields": {
                "type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
                "connection_generation": "远端设备连接代次",
                "executor_generation": "设备执行器代次",
                "interaction_generation": "语音占用代次", "stream_id": "媒体流编号",
                "task_id": "固定语音事务编号", "request_id": "本次合成请求编号",
            },
        })
        await sending
        cancelling = asyncio.create_task(session.cancel(timeout_seconds=1.0))
        cancel = await outbox.next_control()
        assert cancel["type"] == "media.tts.cancel"
        outbox.accept_control({
            "type": "media.tts.cancelled", "protocol_version": 1,
            "device_id": "client-a", "core_session_id": "core-a",
            "connection_generation": 2, "executor_generation": 3,
            "interaction_generation": 4, "stream_id": "media-a",
            "task_id": state.task_id, "request_id": state.request_id,
            "fields": {
                "type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
                "connection_generation": "远端设备连接代次",
                "executor_generation": "设备执行器代次",
                "interaction_generation": "语音占用代次", "stream_id": "媒体流编号",
                "task_id": "固定语音事务编号", "request_id": "本次合成请求编号",
            },
        })
        assert await cancelling is True
        await session.close()

    asyncio.run(scenario())


def test_client_aware_provider_uses_host_tts_when_active_device_lacks_client_capability() -> None:
    host = _HostTts()
    adaptive = ClientAwareMiniMaxTtsClient(
        runtime=SimpleNamespace(active_remote_client_tts_outbox=lambda: None),
        fallback=host,
        config=SimpleNamespace(
            online_sample_rate=32_000, sample_rate=32_000,
            default_emotion="calm", model="speech-2.8-hd", voice_id="voice-a",
        ),
    )
    router = TtsBackendRouter(remote=adaptive, backend="minimax")
    assert router.bind() == ("minimax", host)


def test_runtime_can_initialize_online_tts_from_active_client_credentials_without_host_key(
    tmp_path: Path, monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "config" / "persona"),
        )
        settings = ProductSettingsStore(
            path=tmp_path / "config" / "product-settings.json", config=config,
        )
        runtime = SessionRuntime(
            config, product_settings=settings, runtime_data_dir=tmp_path / "data",
        )
        lease = InteractionLease(
            device_id="client-a", interaction_generation=4,
            connection_generation=2, executor_generation=3,
        )
        outbox = RemoteClientTtsOutbox(
            lease=lease, core_session_id=runtime.core_session_id, stream_id="media-a",
        )
        runtime.active_remote_client_tts_outbox = lambda: (lease, outbox)
        runtime._ensure_player = lambda: None
        output = SimpleNamespace(tts=None)
        runtime.controller = SimpleNamespace(tts=None, output=output)
        assert await runtime.refresh_client_tts_service() is True
        assert runtime.state.services["tts"] == "READY"
        assert runtime.tts is not None
        _, bound = runtime.tts.bind()
        assert bound.uses_client_tts is True
        await runtime.tts.shutdown()
        await outbox.close()

    asyncio.run(scenario())
