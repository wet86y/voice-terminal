"""Remote client direct TTS relay（远端客户端直放回执）专用测试。"""
from __future__ import annotations

import asyncio

import pytest

from voice_terminal.devices.client_tts import RemoteClientTtsError, RemoteClientTtsOutbox
from voice_terminal.devices.media_protocol import MediaProtocolError, create_tts_control
from voice_terminal.devices.registry import InteractionLease


def _outbox() -> RemoteClientTtsOutbox:
    lease = InteractionLease(
        device_id="client-a", interaction_generation=4,
        connection_generation=2, executor_generation=3,
    )
    return RemoteClientTtsOutbox(lease=lease, core_session_id="core-a", stream_id="media-a")


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


async def _authorized_request(outbox: RemoteClientTtsOutbox):
    authorization_id = await outbox.authorize(
        playback_key="speech-a", credential="test-only-service-key", ttl_ms=60_000,
    )
    grant = await outbox.next_control()
    assert grant["type"] == "media.tts.authorize"
    outbox.accept_control(_receipt(
        outbox, "media.tts.authorization_accepted",
        authorization_id=authorization_id, playback_key="speech-a",
    ))
    state = await outbox.submit(
        authorization_id=authorization_id, playback_key="speech-a", request_id="request-a",
        segment_index=0, playback_start_sample=0, text="请播放。", model="speech-2.8-hd",
        voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
    )
    request = await outbox.next_control()
    assert request["type"] == "media.tts.request"
    assert "credential" not in request
    return authorization_id, state


def test_outbox_accepts_only_keyed_v2_device_output_receipts() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id, state = await _authorized_request(outbox)
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
        ))
        bad = _receipt(
            outbox, "media.tts.output_progress", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            progress_sequence=1, generated_samples=3_200, accepted_samples=3_200,
            played_samples=1_600, buffered_samples=1_600, output_complete=False, device_time_us=1_000,
        )
        bad["executor_generation"] = 9
        with pytest.raises(MediaProtocolError, match="媒体租约"):
            outbox.accept_control(bad)
        outbox.accept_control(_receipt(
            outbox, "media.tts.segment_finished", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            text_chars=4, start_sample=0, end_sample=3_200,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            progress_sequence=1, generated_samples=3_200, accepted_samples=3_200,
            played_samples=1_600, buffered_samples=1_600, output_complete=False, device_time_us=1_000,
        ))
        assert not state.finished.is_set()
        outbox.accept_control(_receipt(
            outbox, "media.tts.output_progress", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            progress_sequence=2, generated_samples=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=2_000,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.completed", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            total_samples=3_200, playback_end_sample=3_200, accepted_samples=3_200,
            played_samples=3_200, buffered_samples=0, output_complete=True, device_time_us=2_100,
        ))
        assert state.finished.is_set()
        with pytest.raises(RemoteClientTtsError):
            await outbox.submit(
                authorization_id=authorization_id, playback_key="speech-a", request_id="duplicate-a",
                segment_index=0, playback_start_sample=0, text="请播放。", model="speech-2.8-hd",
                voice_id="voice-a", sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
            )
        await outbox.close()

    asyncio.run(scenario())


def test_cancel_is_bound_to_same_key_and_waits_for_device_stop_receipt() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id, state = await _authorized_request(outbox)
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
        ))
        task = asyncio.create_task(outbox.cancel(state.request_id, reason="replace", timeout_seconds=1))
        cancel = await outbox.next_control()
        assert cancel["type"] == "media.tts.cancel"
        assert cancel["playback_key"] == "speech-a"
        assert cancel["reason"] == "replace"
        outbox.accept_control(_receipt(
            outbox, "media.tts.cancelled", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            accepted_samples=1_600, played_samples=1_600, buffered_samples=0,
            output_complete=False, device_time_us=3_000,
        ))
        assert await task is True
        await outbox.close()

    asyncio.run(scenario())


def test_failure_receipt_preserves_actual_device_progress_without_claiming_completion() -> None:
    async def scenario():
        outbox = _outbox()
        authorization_id, state = await _authorized_request(outbox)
        outbox.accept_control(_receipt(
            outbox, "media.tts.accepted", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0, accepted=True,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.started", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
        ))
        outbox.accept_control(_receipt(
            outbox, "media.tts.failed", authorization_id=authorization_id,
            playback_key="speech-a", request_id=state.request_id, segment_index=0,
            error="设备输出未确认。", accepted_samples=3_200, played_samples=1_600,
            buffered_samples=0, output_complete=False, device_time_us=4_000,
        ))
        assert state.finished.is_set() and state.error == "设备输出未确认。"
        assert outbox.playback_progress("speech-a") == {
            "sample_rate": 32_000,
            "accepted_samples": 3_200, "played_samples": 1_600, "buffered_samples": 0,
            "output_complete": False, "device_time_us": 4_000,
        }
        await outbox.close()

    asyncio.run(scenario())
