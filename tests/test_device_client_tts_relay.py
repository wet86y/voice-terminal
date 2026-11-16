"""Remote client TTS relay（远端客户端合成中继）专项验收。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.devices.client_tts import (
    RemoteClientTtsOutbox,
    RemoteClientTtsSession,
)
from voice_terminal.devices.media_protocol import ClientTtsAudioFrame, MediaProtocolError
from voice_terminal.devices.registry import InteractionLease


def _lease(**overrides) -> InteractionLease:
    values = {
        "device_id": "client-a",
        "interaction_generation": 4,
        "connection_generation": 2,
        "executor_generation": 3,
    }
    values.update(overrides)
    return InteractionLease(**values)


def _frame(outbox: RemoteClientTtsOutbox, state, **overrides) -> ClientTtsAudioFrame:
    values = {
        "device_id": outbox.lease.device_id,
        "core_session_id": outbox.core_session_id,
        "connection_generation": outbox.lease.connection_generation,
        "executor_generation": outbox.lease.executor_generation,
        "interaction_generation": outbox.lease.interaction_generation,
        "stream_id": outbox.stream_id,
        "task_id": state.task_id,
        "request_id": state.request_id,
        "sequence": 0,
        "start_sample": 0,
        "sample_count": 320,
        "sample_rate": state.sample_rate,
        "device_time_us": 1000,
        "payload": b"\x02\x00" * 320,
    }
    values.update(overrides)
    return ClientTtsAudioFrame(**values)


def _status(outbox, state, kind: str, **extra):
    value = {
        "type": kind,
        "protocol_version": 1,
        "device_id": outbox.lease.device_id,
        "core_session_id": outbox.core_session_id,
        "connection_generation": outbox.lease.connection_generation,
        "executor_generation": outbox.lease.executor_generation,
        "interaction_generation": outbox.lease.interaction_generation,
        "stream_id": outbox.stream_id,
        "task_id": state.task_id,
        "request_id": state.request_id,
    }
    value.update(extra)
    value["fields"] = {
        key: {
            "type": "控制消息类型", "protocol_version": "设备媒体协议版本",
            "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
            "connection_generation": "远端设备连接代次",
            "executor_generation": "远端设备执行器代次",
            "interaction_generation": "核心授予的语音占用代次",
            "stream_id": "连续音频流编号", "task_id": "固定语音播放事务编号",
            "request_id": "客户端合成请求编号", "text_chars": "已完成文本字符数",
            "end_sample": "文本段结束采样位置", "total_samples": "任务总有效采样数",
            "error": "不含凭据和文本的错误摘要", "event": "提供方事件类型",
            "sample_cursor": "提供方事件对应的采样位置",
            "sentence_index": "句子顺序编号", "sentence_start_sample": "句子起始采样位置",
            "alignment": "经结构边界验证的字词对齐数据",
        }[key]
        for key in value if key != "fields"
    }
    return value


def test_remote_client_tts_outbox_binds_request_to_lease_without_credentials() -> None:
    async def scenario():
        outbox = RemoteClientTtsOutbox(
            lease=_lease(), core_session_id="core-a", stream_id="media-a",
        )
        state = await outbox.submit(
            task_id="turn-1.1", text="你好。", model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, speed=1.0, volume=1.0, emotion="calm",
        )
        request = await outbox.next_control()
        assert request["type"] == "media.tts.request"
        assert request["request_id"] == state.request_id
        assert request["device_id"] == "client-a"
        assert request["interaction_generation"] == 4
        assert "api_key" not in request
        outbox.accept_control(_status(outbox, state, "media.tts.started"))
        await outbox.accept_audio(_frame(outbox, state))
        assert await state.audio.get() == b"\x02\x00" * 320
        outbox.accept_control(_status(
            outbox, state, "media.tts.segment_finished", text_chars=3, end_sample=320,
        ))
        outbox.accept_control(_status(outbox, state, "media.tts.completed", total_samples=320))
        assert state.finished.is_set()
        assert state.segment_receipt == {"text_chars": 3, "end_sample": 320}

    asyncio.run(scenario())


def test_remote_client_tts_outbox_rejects_stale_generation_and_discontinuous_audio() -> None:
    async def scenario():
        outbox = RemoteClientTtsOutbox(
            lease=_lease(), core_session_id="core-a", stream_id="media-a",
        )
        state = await outbox.submit(
            task_id="turn-1.1", text="你好。", model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
        )
        await outbox.next_control()
        outbox.accept_control(_status(outbox, state, "media.tts.started"))
        with pytest.raises(MediaProtocolError, match="代次"):
            await outbox.accept_audio(_frame(outbox, state, executor_generation=4))
        await outbox.accept_audio(_frame(outbox, state))
        with pytest.raises(MediaProtocolError, match="序号"):
            await outbox.accept_audio(_frame(outbox, state, sequence=2, start_sample=320))

    asyncio.run(scenario())


def test_remote_client_tts_session_streams_pcm_and_waits_for_confirmed_completion() -> None:
    async def scenario():
        outbox = RemoteClientTtsOutbox(
            lease=_lease(), core_session_id="core-a", stream_id="media-a",
        )
        async def bind_remote_playback_target(playback_key, lease):
            return playback_key == "turn-1.1" and lease == outbox.lease

        session = RemoteClientTtsSession(
            runtime=SimpleNamespace(bind_remote_playback_target=bind_remote_playback_target),
            lease=outbox.lease, outbox=outbox, playback_key="turn-1.1",
            model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, emotion="calm",
        )
        await session.start()
        consumer = asyncio.create_task(_collect_audio(session))
        sending = asyncio.create_task(session.send_text("你好。"))
        request = await outbox.next_control()
        state = outbox.request(request["request_id"])
        assert state is not None
        outbox.accept_control(_status(outbox, state, "media.tts.started"))
        await sending
        # send_text returns only after the client confirmed task_started.
        accepted = asyncio.create_task(session.send_text("再见。"))
        await asyncio.sleep(0)
        await outbox.accept_audio(_frame(outbox, state))
        outbox.accept_control(_status(
            outbox, state, "media.tts.segment_finished", text_chars=3, end_sample=320,
        ))
        outbox.accept_control(_status(outbox, state, "media.tts.completed", total_samples=320))
        second_request = await outbox.next_control()
        second_state = outbox.request(second_request["request_id"])
        assert second_state is not None
        outbox.accept_control(_status(outbox, second_state, "media.tts.started"))
        await accepted
        await outbox.accept_audio(_frame(
            outbox, second_state, task_id=second_state.task_id, payload=b"\x03\x00" * 320,
        ))
        outbox.accept_control(_status(
            outbox, second_state, "media.tts.segment_finished", text_chars=3, end_sample=320,
        ))
        outbox.accept_control(_status(outbox, second_state, "media.tts.completed", total_samples=320))
        await session.flush()
        assert await consumer == [b"\x02\x00" * 320, b"\x03\x00" * 320]
        assert session.received_bytes == 1280
        await session.close()

    asyncio.run(scenario())


def test_remote_client_tts_cancel_waits_for_client_confirmation() -> None:
    async def scenario():
        outbox = RemoteClientTtsOutbox(
            lease=_lease(), core_session_id="core-a", stream_id="media-a",
        )
        state = await outbox.submit(
            task_id="turn-1.1", text="请停止。", model="speech-2.8-hd", voice_id="voice-a",
            sample_rate=32_000, speed=1.0, volume=1.0, emotion=None,
        )
        await outbox.next_control()
        outbox.accept_control(_status(outbox, state, "media.tts.started"))
        cancel_task = asyncio.create_task(outbox.cancel(state.request_id, timeout_seconds=1.0))
        cancel = await outbox.next_control()
        assert cancel["type"] == "media.tts.cancel"
        assert cancel["request_id"] == state.request_id
        outbox.accept_control(_status(outbox, state, "media.tts.cancelled"))
        assert await cancel_task is True

    asyncio.run(scenario())


async def _collect_audio(session: RemoteClientTtsSession) -> list[bytes]:
    return [chunk async for chunk in session.audio_chunks()]
