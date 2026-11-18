"""Client-aware TTS provider（由服务端凭据授权客户端直放）测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.devices.client_tts import RemoteClientTtsOutbox
from voice_terminal.devices.media_protocol import create_tts_control
from voice_terminal.devices.registry import InteractionLease
from voice_terminal.providers.client_tts import ClientAwareMiniMaxTtsClient
from voice_terminal.tts.controls import SpeechControls
from voice_terminal.tts.router import TtsBackendRouter


class _HostTts:
    transport = "websocket"
    scheduling = "direct"
    effective_scheduling = "direct"

    def session(self, **kwargs):
        return kwargs

    async def close(self):
        return None


def _lease() -> InteractionLease:
    return InteractionLease(
        device_id="client-a", interaction_generation=4,
        connection_generation=2, executor_generation=3,
    )


def _identity(outbox: RemoteClientTtsOutbox) -> dict[str, object]:
    return {
        "device_id": outbox.lease.device_id, "core_session_id": outbox.core_session_id,
        "connection_generation": outbox.lease.connection_generation,
        "executor_generation": outbox.lease.executor_generation,
        "interaction_generation": outbox.lease.interaction_generation,
        "stream_id": outbox.stream_id,
    }


def _receipt(outbox, kind: str, **extra):
    return create_tts_control(kind, _identity(outbox), **extra)


def test_router_uses_service_credential_for_temporary_direct_playback_authorization() -> None:
    async def scenario():
        lease = _lease()
        outbox = RemoteClientTtsOutbox(lease=lease, core_session_id="core-a", stream_id="media-a")

        async def bind_remote_playback_target(playback_key, candidate):
            return playback_key == "turn-a:2" and candidate == lease

        runtime = SimpleNamespace(
            active_remote_client_tts_outbox=lambda: (lease, outbox),
            bind_remote_playback_target=bind_remote_playback_target,
            _api_key=lambda environment: "service-secret-for-test" if environment == "MINIMAX_API_KEY" else "",
        )
        config = SimpleNamespace(
            online_sample_rate=32_000, sample_rate=32_000, model="speech-2.8-hd",
            voice_id="voice-a", default_emotion="calm", api_key_env="MINIMAX_API_KEY",
        )
        adaptive = ClientAwareMiniMaxTtsClient(runtime=runtime, fallback=None, config=config)
        router = TtsBackendRouter(remote=adaptive, backend="minimax")
        backend, client = router.bind()
        assert backend == "minimax"
        assert client.uses_client_tts is True
        assert client.direct_playback is True
        session = client.session(
            playback_key="turn-a:2", turn_id="turn-a", speech_id="speech-a",
            controls=SpeechControls(guidance="happy", speed=1.2, volume=0.8),
        )
        starting = asyncio.create_task(session.start())
        authorization = await outbox.next_control()
        assert authorization["type"] == "media.tts.authorize"
        assert authorization["credential"] == "service-secret-for-test"
        assert authorization["provider"] == "minimax"
        assert authorization["playback_key"] == "turn-a:2"
        authorization_id = authorization["authorization_id"]
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted",
            authorization_id=authorization_id, playback_key="turn-a:2",
        ))
        await starting
        sending = asyncio.create_task(session.send_text("客户端语音"))
        request = await outbox.next_control()
        assert request["type"] == "media.tts.request"
        assert request["model"] == "speech-2.8-hd"
        assert request["voice_id"] == "voice-a"
        assert request["sample_rate"] == 32_000
        assert request["speed"] == 1.2
        assert request["volume"] == 0.8
        assert request["emotion"] == "happy"
        assert "credential" not in request and "api_key" not in request
        state = outbox.request(request["request_id"])
        assert state is not None
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="turn-a:2", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="turn-a:2", request_id=state.request_id, segment_index=0,
        ))
        await sending
        stopping = asyncio.create_task(session.cancel(timeout_seconds=1))
        cancel = await outbox.next_control()
        assert cancel["type"] == "media.tts.cancel"
        assert cancel["playback_key"] == "turn-a:2"
        outbox.accept_control(_receipt(
            outbox, "media.tts.cancelled", authorization_id=authorization_id,
            playback_key="turn-a:2", request_id=state.request_id, segment_index=0,
            accepted_samples=0, played_samples=0, buffered_samples=0,
            output_complete=False, device_time_us=100,
        ))
        assert await stopping is True
        await session.close()
        revoke = await outbox.next_control()
        assert revoke["type"] == "media.tts.authorization_revoked"
        await outbox.close()

    asyncio.run(scenario())


def test_client_local_credentials_cannot_replace_missing_service_credential() -> None:
    lease = _lease()
    outbox = RemoteClientTtsOutbox(lease=lease, core_session_id="core-a", stream_id="media-a")
    runtime = SimpleNamespace(
        active_remote_client_tts_outbox=lambda: (lease, outbox),
        _api_key=lambda _environment: "",
    )
    # Even a fallback/client object with a local credential is not a valid source for a remote grant.
    local_fallback = SimpleNamespace(client=SimpleNamespace(api_key="client-local-secret"))
    config = SimpleNamespace(
        online_sample_rate=32_000, sample_rate=32_000, model="speech-2.8-hd",
        voice_id="voice-a", default_emotion="calm", api_key_env="MINIMAX_API_KEY",
    )
    adaptive = ClientAwareMiniMaxTtsClient(runtime=runtime, fallback=local_fallback, config=config)
    with pytest.raises(RuntimeError, match="服务端没有 MiniMax 凭据"):
        adaptive.bind_speech_client()


def test_router_keeps_local_backend_when_no_remote_client_tts_lease_is_active() -> None:
    host = _HostTts()
    adaptive = ClientAwareMiniMaxTtsClient(
        runtime=SimpleNamespace(active_remote_client_tts_outbox=lambda: None),
        fallback=host,
        config=SimpleNamespace(
            online_sample_rate=32_000, sample_rate=32_000,
            default_emotion="calm", model="speech-2.8-hd", voice_id="voice-a",
            api_key_env="MINIMAX_API_KEY",
        ),
    )
    router = TtsBackendRouter(remote=adaptive, backend="minimax")
    assert router.bind() == ("minimax", host)
