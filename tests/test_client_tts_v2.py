"""Client direct TTS v2（客户端直放语音协议第 2 版）回归。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voice_terminal.devices.client_tts import RemoteClientTtsError, RemoteClientTtsOutbox, RemoteClientTtsSession
from voice_terminal.devices.media_protocol import (
    ClientTtsAudioFrame,
    MediaProtocolError,
    create_tts_control,
    encode_client_tts_audio_frame,
    parse_tts_control,
)
from voice_terminal.devices.registry import InteractionLease


def _lease() -> InteractionLease:
    return InteractionLease(
        device_id="client-a", interaction_generation=4,
        connection_generation=2, executor_generation=3,
    )


def _identity(outbox: RemoteClientTtsOutbox) -> dict[str, object]:
    return {
        "device_id": outbox.lease.device_id,
        "core_session_id": outbox.core_session_id,
        "connection_generation": outbox.lease.connection_generation,
        "executor_generation": outbox.lease.executor_generation,
        "interaction_generation": outbox.lease.interaction_generation,
        "stream_id": outbox.stream_id,
    }


def _receipt(outbox: RemoteClientTtsOutbox, kind: str, **extra):
    return create_tts_control(kind, _identity(outbox), **extra)


def _outbox() -> RemoteClientTtsOutbox:
    return RemoteClientTtsOutbox(lease=_lease(), core_session_id="core-a", stream_id="media-a")


def test_client_tts_segment_with_no_terminal_receipt_expires(monkeypatch) -> None:
    monkeypatch.setattr("voice_terminal.devices.client_tts._SEGMENT_STALL_SECONDS", 0.01)

    async def scenario() -> None:
        session = object.__new__(RemoteClientTtsSession)
        session.outbox = _outbox()
        session.playback_key = "speech-stalled"
        state = SimpleNamespace(finished=asyncio.Event(), reported_generated_samples=0, error=None, cancelled=False)
        session._requests = [state]
        with pytest.raises(RemoteClientTtsError, match="没有继续推进"):
            await asyncio.wait_for(session.wait_for_segment(0), 0.5)
        assert not state.finished.is_set(), "超时后仍需允许正常终态回执或取消请求收尾"

    asyncio.run(scenario())


def test_v2_authorization_is_separate_and_request_has_no_credential() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id = await outbox.authorize(
            playback_key="speech:1", credential="unit-test-server-key", ttl_ms=60_000,
        )
        grant = await outbox.next_control()
        assert grant["type"] == "media.tts.authorize"
        assert grant["protocol_version"] == 4
        assert grant["credential"] == "unit-test-server-key"
        assert grant["fields"]["credential"]
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted",
            authorization_id=authorization_id, playback_key="speech:1",
        ))
        state = await outbox.submit(
            authorization_id=authorization_id, playback_key="speech:1", request_id="request-a",
            segment_index=0, playback_start_sample=0, text="你好。", model="speech-2.8-hd",
            voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion="calm",
        )
        request = await outbox.next_control()
        assert request["type"] == "media.tts.request"
        assert request["protocol_version"] == 4
        assert request["playback_key"] == "speech:1"
        assert request["request_id"] == state.request_id
        assert "credential" not in request
        assert "api_key" not in request
        await outbox.close()

    asyncio.run(scenario())


def test_v2_outbox_requires_real_keyed_output_before_completed_receipt() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id = await outbox.authorize(
            playback_key="speech:2", credential="test-only-key", ttl_ms=60_000,
        )
        await outbox.next_control()
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted",
            authorization_id=authorization_id, playback_key="speech:2",
        ))
        state = await outbox.submit(
            authorization_id=authorization_id, playback_key="speech:2", request_id="request-b",
            segment_index=0, playback_start_sample=0, text="听见这句话。", model="speech-2.8-hd",
            voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
        )
        await outbox.next_control()
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.segment_finished", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0,
            text_chars=6, start_sample=0, end_sample=3_200,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0,
            progress_sequence=1, generated_samples=3_200, accepted_samples=3_200,
            played_samples=1_600, buffered_samples=1_600, output_complete=False, device_time_us=200,
        ))
        assert not state.finished.is_set()
        with pytest.raises(MediaProtocolError, match="设备输出证据"):
            outbox.accept_control(_receipt(
                outbox, "media.tts.completed", authorization_id=authorization_id,
                playback_key="speech:2", request_id=state.request_id, segment_index=0,
                total_samples=3_200, playback_end_sample=3_200, accepted_samples=3_200,
                played_samples=1_600, buffered_samples=1_600, output_complete=False, device_time_us=200,
            ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0,
            progress_sequence=2, generated_samples=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=300,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.completed", authorization_id=authorization_id,
            playback_key="speech:2", request_id=state.request_id, segment_index=0,
            total_samples=3_200, playback_end_sample=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=301,
        ))
        assert state.finished.is_set()
        assert outbox.playback_progress("speech:2") == {
            "sample_rate": 32_000,
            "accepted_samples": 3_200, "played_samples": 3_200, "buffered_samples": 0,
            "output_complete": True, "device_time_us": 301,
        }
        await outbox.close()

    asyncio.run(scenario())


def test_v2_duplicate_producer_stale_generation_and_short_audio_are_rejected() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id = await outbox.authorize(
            playback_key="speech:3", credential="unit-key", ttl_ms=60_000,
        )
        await outbox.next_control()
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted",
            authorization_id=authorization_id, playback_key="speech:3",
        ))
        state = await outbox.submit(
            authorization_id=authorization_id, playback_key="speech:3", request_id="request-c",
            segment_index=0, playback_start_sample=0, text="你好。", model="speech-2.8-hd",
            voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
        )
        await outbox.next_control()
        with pytest.raises(RemoteClientTtsError):
            await outbox.submit(
                authorization_id=authorization_id, playback_key="speech:3", request_id="request-c2",
                segment_index=0, playback_start_sample=0, text="你好。", model="speech-2.8-hd",
                voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
            )
        stale = _receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech:3", request_id=state.request_id, segment_index=0,
        )
        stale["interaction_generation"] = 5
        stale["fields"]["interaction_generation"] = "旧交互代次"
        with pytest.raises(MediaProtocolError, match="身份"):
            outbox.accept_control(stale)
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech:3", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech:3", request_id=state.request_id, segment_index=0,
        ))
        with pytest.raises(MediaProtocolError, match="最小音频时长"):
            outbox.accept_control(_receipt(
                outbox, "media.tts.segment_finished", authorization_id=authorization_id,
                playback_key="speech:3", request_id=state.request_id, segment_index=0,
                text_chars=3, start_sample=0, end_sample=100,
            ))
        await outbox.close()

    asyncio.run(scenario())


def test_session_never_exposes_pcm_and_waits_for_device_output_receipt() -> None:
    async def scenario():
        outbox = _outbox()

        async def bind_remote_playback_target(playback_key, lease):
            return playback_key == "speech:4" and lease == outbox.lease

        session = RemoteClientTtsSession(
            runtime=SimpleNamespace(bind_remote_playback_target=bind_remote_playback_target),
            lease=outbox.lease, outbox=outbox, playback_key="speech:4",
            credential="server-only-test-key", model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, emotion="calm",
        )
        starting = asyncio.create_task(session.start())
        grant = await outbox.next_control()
        assert grant["type"] == "media.tts.authorize"
        assert grant["credential"] == "server-only-test-key"
        auth_id = session.authorization_id
        assert auth_id is not None
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted", authorization_id=auth_id, playback_key="speech:4",
        ))
        await starting

        sending = asyncio.create_task(session.send_text("音频只在客户端输出。"))
        request = await outbox.next_control()
        assert "credential" not in request
        state = outbox.request(request["request_id"])
        assert state is not None
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0,
        ))
        await sending

        waiting = asyncio.create_task(session.wait_for_segment(0))
        outbox.accept_control(_receipt(
            outbox, "media.tts.segment_finished", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0,
            text_chars=10, start_sample=0, end_sample=3_200,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0,
            progress_sequence=1, generated_samples=3_200, accepted_samples=3_200,
            played_samples=1_600, buffered_samples=1_600, output_complete=False, device_time_us=100,
        ))
        await asyncio.sleep(0)
        assert not waiting.done()
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0,
            progress_sequence=2, generated_samples=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=200,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.completed", authorization_id=auth_id,
            playback_key="speech:4", request_id=state.request_id, segment_index=0,
            total_samples=3_200, playback_end_sample=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=201,
        ))
        receipt = await waiting
        assert receipt["end_sample"] == 3_200
        assert receipt["end_received_bytes"] == 6_400
        assert await session.wait_until_output_complete() == {
            "sample_rate": 32_000,
            "accepted_samples": 3_200, "played_samples": 3_200, "buffered_samples": 0,
            "output_complete": True, "device_time_us": 201,
        }
        with pytest.raises(RuntimeError, match="禁止读取或回传 PCM"):
            async for _chunk in session.audio_chunks():
                pass
        await session.close()
        revoke = await outbox.next_control()
        assert revoke["type"] == "media.tts.authorization_revoked"
        assert revoke["playback_key"] == "speech:4"
        await outbox.close()

    asyncio.run(scenario())


def test_send_text_waits_for_started_after_accepted_arrives_first() -> None:
    """accepted 先到时 send_text 仍要继续等 started，而不能提前报失败。"""
    async def scenario():
        outbox = _outbox()

        async def bind_remote_playback_target(playback_key, lease):
            return playback_key == "speech:first-accepted" and lease == outbox.lease

        session = RemoteClientTtsSession(
            runtime=SimpleNamespace(bind_remote_playback_target=bind_remote_playback_target),
            lease=outbox.lease, outbox=outbox, playback_key="speech:first-accepted",
            credential="test-only-service-key", model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, emotion=None,
        )
        starting = asyncio.create_task(session.start())
        grant = await outbox.next_control()
        authorization_id = grant["authorization_id"]
        outbox.accept_control(_receipt(
            outbox, "media.tts.authorization_accepted", authorization_id=authorization_id,
            playback_key="speech:first-accepted",
        ))
        await starting

        sending = asyncio.create_task(session.send_text("接受回执先到。"))
        request = await outbox.next_control()
        state = outbox.request(request["request_id"])
        assert state is not None
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech:first-accepted", request_id=state.request_id,
            segment_index=0, accepted=True,
        ))
        # Explicitly yield after the first event so the waiter observes the
        # ordering that previously made FIRST_COMPLETED return too early.
        await asyncio.sleep(0)
        assert not sending.done()
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech:first-accepted", request_id=state.request_id,
            segment_index=0,
        ))
        await asyncio.wait_for(sending, 1)
        outbox.accept_control(_receipt(
            outbox, "media.tts.failed", authorization_id=authorization_id,
            playback_key="speech:first-accepted", request_id=state.request_id,
            segment_index=0, error="test terminal failure",
        ))
        await session.close()
        revoke = await outbox.next_control()
        assert revoke["type"] == "media.tts.authorization_revoked"
        await outbox.close()

    asyncio.run(scenario())


def test_v2_parser_rejects_legacy_pcm_and_duplicate_json_keys() -> None:
    frame = ClientTtsAudioFrame(
        device_id="client-a", core_session_id="core-a", connection_generation=2,
        executor_generation=3, interaction_generation=4, stream_id="media-a",
        task_id="task-a", request_id="request-a", sequence=0, start_sample=0,
        sample_count=320, sample_rate=16_000, device_time_us=1_000,
        payload=b"\x00\x00" * 320,
    )
    with pytest.raises(MediaProtocolError, match="禁止客户端合成 PCM 回传"):
        encode_client_tts_audio_frame(frame)
    with pytest.raises(MediaProtocolError, match="重复字段"):
        parse_tts_control('{"type":"media.tts.request","type":"media.tts.request"}')
