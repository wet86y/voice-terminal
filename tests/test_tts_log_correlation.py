"""本地 TTS 日志关联：轮次/事务标识、失败原因与文本隐私。"""

import asyncio
import base64
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsError
from voice_terminal.tts.voices import ResolvedLocalTtsVoice

LOGGER = "voice_terminal.providers.audio_cpp_tts"
TEXT = "这是一个不少于十个有效字符的本地语音块。"


def _event(event_type: str, **fields) -> str:
    return "data: " + json.dumps({"type": event_type, **fields}) + "\n\n"


def _audio_event(pcm: bytes) -> str:
    return _event("speech.audio.delta", audio=base64.b64encode(pcm).decode("ascii"))


def _response(*events: str) -> httpx.Response:
    return httpx.Response(200, content="".join(events).encode("utf-8"),
                          headers={"content-type": "text/event-stream"})


def _fake_client(handler, *, voice: bool = True) -> AudioCppTtsClient:
    server = SimpleNamespace(
        base_url="http://127.0.0.1:18881",
        model_id="voxcpm1-stream",
        native_sample_rate=16000,
        local_model="voxcpm1",
        ensure_ready=AsyncMock(),
        close=AsyncMock(),
    )
    client = AudioCppTtsClient(server, http_transport=httpx.MockTransport(handler),
                               gpu_max_performance=False)
    if voice:
        client.resolve_voice = lambda emotion, preview=False: ResolvedLocalTtsVoice(
            model="voxcpm1", native_model_id="voxcpm1-stream", voice_id="test-voice",
            revision=3, reference_path=Path("test-reference.wav").resolve(),
            reference_text="测试参考音频对应文本", style_description="", emotion=emotion,
            is_preview=preview,
        )
    return client


def _records(caplog, event: str) -> list[str]:
    return [item.getMessage() for item in caplog.records if f"audio_cpp_tts.{event}" in item.getMessage()]


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return _response(_audio_event(b"\x01\x00" * 8), _event("speech.audio.done"), "data: [DONE]\n\n")


def test_session_and_request_logs_carry_turn_speech_and_session_ids(caplog):
    async def scenario():
        client = _fake_client(_ok_handler)
        session = client.session(turn_id="turn-7", speech_id="speech-9")
        await session.start()
        await session.send_text(TEXT)
        await session.wait_for_segment(0)
        await session.send_text(TEXT + "第二段")
        await session.wait_for_segment(1)
        await session.finish()
        return session

    with caplog.at_level(logging.INFO, logger=LOGGER):
        session = asyncio.run(scenario())

    policy = _records(caplog, "session_policy")
    starts = _records(caplog, "request_start")
    completes = _records(caplog, "request_complete")
    assert len(policy) == 1 and len(starts) == 2 and len(completes) == 2
    for line in (policy[0], *starts, *completes):
        assert "turn=turn-7" in line
        assert "speech=speech-9" in line
        assert f"session={session.session_tag}" in line
    assert "segment=0" in starts[0] and "segment=1" in starts[1]
    assert "segment_index=0" in completes[0] and "segment_index=1" in completes[1]
    # 既有口径不破坏：合成指标仍在同一行上。
    assert "rtf=" in completes[0] and "ttfb_ms=" in completes[0]
    assert "inference_steps=" in policy[0]
    # 参考配方必须落盘：现场取不到该字段时无法判断高保真克隆通道是否生效
    # （本地 TTS 变慢排查中曾因缺此字段只能推断，见 local-tts 诊断）。
    for line in completes:
        assert "reference_recipe=" in line
        assert "reference_recipe=None" not in line


def test_text_content_stays_out_of_logs_by_default(caplog):
    async def scenario():
        client = _fake_client(_ok_handler)
        session = client.session(turn_id="turn-privacy", speech_id="speech-1")
        await session.start()
        await session.send_text(TEXT)
        await session.wait_for_segment(0)
        await session.finish()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    starts = _records(caplog, "request_start")
    assert "text_digest=" in starts[0]
    assert f"chars={len(TEXT)}" in starts[0]
    assert TEXT not in caplog.text
    assert "text=" not in caplog.text


def test_text_preview_is_opt_in(monkeypatch, caplog):
    monkeypatch.setenv("VOICE_TTS_LOG_TEXT", "1")

    async def scenario():
        client = _fake_client(_ok_handler)
        session = client.session(turn_id="turn-text", speech_id="speech-2")
        await session.start()
        await session.send_text(TEXT)
        await session.wait_for_segment(0)
        await session.finish()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    assert TEXT[:20] in caplog.text


def test_sse_error_event_logs_request_failed(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(_event("error", message="native decode failed"), "data: [DONE]\n\n")

    async def scenario():
        client = _fake_client(handler)
        session = client.session(turn_id="turn-err", speech_id="speech-3")
        await session.start()
        with pytest.raises(AudioCppTtsError):
            await session.send_text(TEXT)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    failures = _records(caplog, "request_failed")
    assert len(failures) == 1
    line = failures[0]
    assert "turn=turn-err" in line and "speech=speech-3" in line
    assert "error_type=AudioCppTtsError" in line
    assert "stage=stream" in line
    assert "retry=False" in line


def test_missing_audio_logs_request_failed(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return _response("data: [DONE]\n\n")

    async def scenario():
        client = _fake_client(handler)
        session = client.session(turn_id="turn-empty", speech_id="speech-4")
        await session.start()
        with pytest.raises(AudioCppTtsError):
            await session.send_text(TEXT)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    failures = _records(caplog, "request_failed")
    assert len(failures) == 1
    assert "stage=finalize" in failures[0]
    assert "没有返回 PCM" in failures[0] or "PCM" in failures[0]


def test_unconfigured_voice_logs_request_rejected(caplog):
    async def scenario():
        client = _fake_client(_ok_handler, voice=False)
        session = client.session(turn_id="turn-voice", speech_id="speech-5")
        await session.start()
        with pytest.raises(AudioCppTtsError):
            await session.send_text(TEXT)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    rejected = _records(caplog, "request_rejected")
    assert len(rejected) == 1
    assert "stage=voice" in rejected[0]
    assert "turn=turn-voice" in rejected[0]


def test_segment_wait_failure_logs_closed_session(caplog):
    async def scenario():
        client = _fake_client(_ok_handler)
        session = client.session(turn_id="turn-wait", speech_id="speech-6")
        await session.start()
        await session.cancel()
        with pytest.raises(AudioCppTtsError):
            await session.wait_for_segment(0)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    waits = _records(caplog, "segment_wait_failed")
    assert len(waits) == 1
    assert "reason=session_closed" in waits[0]
    assert "turn=turn-wait" in waits[0]


def test_segment_wait_timeout_logs_warning(caplog):
    async def scenario():
        client = _fake_client(_ok_handler)
        client.timeout_seconds = 0.05
        session = client.session(turn_id="turn-timeout", speech_id="speech-7")
        await session.start()
        with pytest.raises(AudioCppTtsError):
            await session.wait_for_segment(0)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        asyncio.run(scenario())

    waits = _records(caplog, "segment_wait_failed")
    assert len(waits) == 1
    assert "reason=timeout" in waits[0]


def test_session_factory_keeps_legacy_call_shapes():
    client = _fake_client(_ok_handler)

    assert client.session().turn_id is None
    assert client.session(fresh=True, turn_id="t1").turn_id == "t1"
    assert client.session(turn_id="t2", speech_id="s2").speech_id == "s2"
