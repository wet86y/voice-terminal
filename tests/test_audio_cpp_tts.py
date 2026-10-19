"""VoxCPM1 本地 TTS（文本转语音）协议与运行装配测试。"""

import asyncio
import base64
import io
import json
import struct
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from voice_terminal.config import AppConfig
from voice_terminal.providers.audio_cpp_tts import (
    AudioCppTtsClient,
    AudioCppTtsError,
    AudioCppTtsServer,
)
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.tts.router import TtsBackendRouter
from voice_terminal.paths import AppPaths
from voice_terminal.tts.voices import ResolvedLocalTtsVoice


def _event(event_type: str, **fields) -> str:
    return "data: " + json.dumps({"type": event_type, **fields}) + "\n\n"


def _audio_event(pcm: bytes) -> str:
    return _event("speech.audio.delta", audio=base64.b64encode(pcm).decode("ascii"))


def _response(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        content="".join(events).encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


def _wav_response(pcm: bytes) -> httpx.Response:
    """_wav_response（完整样音响应）：模拟任务接口的原生率 WAV（波形音频）。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(pcm)
    return httpx.Response(200, json={"audio": base64.b64encode(output.getvalue()).decode("ascii")})


def _fake_client(handler, *, native_sample_rate: int = 16000,
                 sample_rate: int | None = None) -> AudioCppTtsClient:
    local_model = "voxcpm1" if native_sample_rate == 16000 else "voxcpm2"
    server = SimpleNamespace(
        base_url="http://127.0.0.1:18881",
        model_id=f"{local_model}-stream",
        native_sample_rate=native_sample_rate,
        local_model=local_model,
        ensure_ready=AsyncMock(),
        close=AsyncMock(),
    )
    client = AudioCppTtsClient(server, http_transport=httpx.MockTransport(handler),
                               gpu_max_performance=False, sample_rate=sample_rate)
    # Tests must explicitly supply the per-request reference snapshot; production no longer
    # falls back to a server-wide default voice.
    client.resolve_voice = lambda emotion, preview=False: ResolvedLocalTtsVoice(
        model=local_model, native_model_id=f"{local_model}-stream", voice_id="test-voice",
        revision=1, reference_path=Path("test-reference.wav").resolve(),
        reference_text="测试参考音频对应文本", style_description="", emotion=emotion,
        is_preview=preview,
    )
    return client


@pytest.mark.parametrize(("native_sample_rate", "capabilities", "expected"), [
    (48000, {"reference_prompt": True, "full_decode": True}, True),
    (48000, {"reference_prompt": True}, False),
    (16000, {"reference_prompt": True, "full_decode": True}, False),
    (48000, {"acoustic_continuation": True}, True),
])
def test_voxcpm2_continuation_is_inferred_only_from_stable_capabilities(
    native_sample_rate, capabilities, expected,
):
    async def scenario():
        client = _fake_client(lambda _request: _response(), native_sample_rate=native_sample_rate)
        client.server.capabilities = capabilities
        await client.ensure_ready()
        assert client._native_continuation is expected
        await client.close()

    asyncio.run(scenario())


def test_sse_pcm_chunks_are_decoded_and_request_uses_streaming_endpoint():
    async def scenario():
        first = b"\x01\x00" * 4
        second = b"\x02\x00" * 3
        requests = []

        async def handler(request):
            requests.append(request)
            return _response(
                _audio_event(first),
                _audio_event(second),
                _event("speech.audio.done"),
                "data: [DONE]\n\n",
            )

        client = _fake_client(handler)
        session = client.session()
        await session.start()
        await session.send_text("这是一个不少于十个有效字符的本地语音块。")
        receipt = await session.wait_for_segment(0)
        await session.finish()
        chunks = [chunk async for chunk in session.audio_chunks()]

        assert b"".join(chunks) == first + second
        assert receipt.index == 0
        assert receipt.text_chars == len("这是一个不少于十个有效字符的本地语音块。")
        assert receipt.end_received_bytes == len(first) + len(second)
        assert session.completed_segments == 1
        assert client._warmed
        assert len(requests) == 1
        assert requests[0].url.path == "/v1/audio/speech"
        payload = json.loads(requests[0].content)
        assert payload == {
            "model": "voxcpm1-stream",
            "input": "这是一个不少于十个有效字符的本地语音块。",
            "voice_ref": str(Path("test-reference.wav").resolve()),
            "reference_text": "测试参考音频对应文本",
            "response_format": "pcm",
            "stream_format": "sse",
            "options": {"retry_badcase": False, "num_inference_steps": 10, "max_tokens": 336},
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_event("error", message="model rejected input"), "合成失败"),
        (_audio_event(b"\x00\x00") + _event("speech.audio.done"), r"\[DONE\]"),
        (_event("speech.audio.delta", audio="%%%") + "data: [DONE]\n\n", "Base64"),
        (_event("speech.audio.delta", audio=base64.b64encode(b"\x00").decode()) + "data: [DONE]\n\n", "int16"),
        ("data: {not-json}\n\n", "无效 SSE JSON"),
    ],
)
def test_sse_errors_and_incomplete_streams_are_reported(body: str, message: str):
    async def scenario():
        async def handler(_request):
            return _response(body)

        session = _fake_client(handler).session()
        await session.start()
        with pytest.raises(AudioCppTtsError, match=message):
            await session.send_text("错误事件验证文本")

    asyncio.run(scenario())


def test_consecutive_text_blocks_are_submitted_in_order():
    async def scenario():
        texts = []

        async def handler(request):
            payload = json.loads(request.content)
            texts.append(payload["input"])
            pcm = (b"\x01\x00" if len(texts) == 1 else b"\x02\x00") * 3
            return _response(_audio_event(pcm), _event("speech.audio.done"), "data: [DONE]\n\n")

        session = _fake_client(handler).session()
        await session.start()
        await session.send_text("第一段完整文本")
        await session.send_text("第二段完整文本")
        await session.finish()
        chunks = [chunk async for chunk in session.audio_chunks()]
        assert texts == ["第一段完整文本", "第二段完整文本"]
        assert b"".join(chunks) == b"\x01\x00" * 3 + b"\x02\x00" * 3
        assert session.completed_segments == 2

    asyncio.run(scenario())


def test_voxcpm2_pcm_is_continuously_downsampled_across_sse_chunks():
    """下行率被显式压到 16 kHz 时，重采样必须跨 SSE 分块连续。"""
    async def scenario():
        native = [3, 6, 9, 12, 15, 18]
        first = struct.pack("<hh", *native[:2])
        second = struct.pack("<hhhh", *native[2:])

        async def handler(_request):
            return _response(
                _audio_event(first), _audio_event(second),
                _event("speech.audio.done"), "data: [DONE]\n\n",
            )

        client = _fake_client(handler, native_sample_rate=48000, sample_rate=16000)
        session = client.session()
        await session.start()
        await session.send_text("验证 VoxCPM2 的连续重采样输出")
        await session.finish()
        output = b"".join([chunk async for chunk in session.audio_chunks()])
        import numpy as np, soxr
        expected = soxr.resample(np.array(native, dtype=np.int16), 48000, 16000, quality="HQ").tobytes()
        assert len(output) == len(expected)
        # 两次独立整数转换的取整/抖动差值最多为两个最低有效位。
        assert np.max(np.abs(np.frombuffer(output, dtype=np.int16).astype(int) - np.frombuffer(expected, dtype=np.int16))) <= 2
        assert session.request_metrics[0]["native_pcm_bytes"] == 12
        assert session.request_metrics[0]["pcm_bytes"] == 4

    asyncio.run(scenario())


def test_voxcpm2_delivers_native_rate_without_downsampling_by_default():
    """缺省下行率取模型原生率：48 kHz 原生输出必须原样送出。

    此前缺省 16000，VoxCPM2 的 48 kHz 被砍到 8 kHz 带宽；实测原生 8–16 kHz
    占 1.1%–14.4% 能量，这部分全部丢失。
    """
    async def scenario():
        native = [3, 6, 9, 12, 15, 18]
        payload = struct.pack("<hhhhhh", *native)

        async def handler(_request):
            return _response(
                _audio_event(payload[:4]), _audio_event(payload[4:]),
                _event("speech.audio.done"), "data: [DONE]\n\n",
            )

        client = _fake_client(handler, native_sample_rate=48000)
        assert client.sample_rate == 48000
        session = client.session()
        assert session._resampler is None, "原生率下行不应创建重采样器"
        await session.start()
        await session.send_text("验证 VoxCPM2 原生率直通输出")
        await session.finish()
        output = b"".join([chunk async for chunk in session.audio_chunks()])
        assert output == payload
        assert session.request_metrics[0]["native_pcm_bytes"] == 12
        assert session.request_metrics[0]["pcm_bytes"] == 12

        # VoxCPM1 的原生率本来就是 16 kHz。
        client_v1 = _fake_client(handler, native_sample_rate=16000)
        assert client_v1.sample_rate == 16000

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("emotion", "instruction"),
    [
        ("calm", "语气平静自然"), ("happy", "语气开心轻快"), ("sad", "语气悲伤低落"),
        ("angry", "语气愤怒"), ("fearful", "语气紧张害怕"),
        ("disgusted", "语气厌恶"), ("surprised", "语气惊讶"),
    ],
)
def test_voxcpm2_ignores_legacy_emotions_and_formal_style_fields(emotion, instruction):
    async def scenario():
        requests = []

        async def handler(request):
            requests.append(request)
            return _response(_audio_event(b"\x01\x00" * 8),
                             _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler, native_sample_rate=48000)
        client.resolve_voice = lambda selected, preview=False: ResolvedLocalTtsVoice(
            model="voxcpm2", native_model_id="voxcpm2-stream", voice_id="voice-id",
            revision=3, reference_path=Path("voice2.wav").resolve(), reference_text="准确的参考转写。",
            style_description="语速适中", emotion=selected, is_preview=preview,
        )
        session = client.session(emotion=emotion)
        await session.start()
        original = "这段正文用于验证事务回执只计算用户看到的文字。"
        await session.send_text(original)
        receipt = await session.wait_for_segment(0)
        await session.finish()
        _ = [chunk async for chunk in session.audio_chunks()]
        payload = json.loads(requests[0].content)
        assert payload["input"] == original
        assert payload["options"]["num_inference_steps"] == 20
        assert payload["voice_ref"] == str(Path("voice2.wav").resolve())
        assert payload["reference_text"] == "准确的参考转写。"
        assert payload["prompt_audio"] == payload["voice_ref"]
        assert "voice" not in payload and "style" not in payload
        assert receipt.text_chars == len(original)

    asyncio.run(scenario())


def test_voxcpm2_design_candidate_has_no_reference_and_preview_does_not_mark_formal_voice_warm():
    async def scenario():
        requests = []

        async def handler(request):
            requests.append(request)
            response = _wav_response(b"\x02\x00" * 8)
            return httpx.Response(200, json={**response.json(), "timing": {
                "wall_ms": 1234.5, "audio_duration_ms": 1600.0, "rtf": 0.7715625,
            }})

        client = _fake_client(handler, native_sample_rate=48000)
        design = ResolvedLocalTtsVoice(
            model="voxcpm2", native_model_id="voxcpm2-stream", voice_id=None, revision=None,
            reference_path=None, reference_text="", style_description="沉稳自然",
            emotion=None, reference_mode="design", is_preview=True,
        )
        session = client.session(voice_snapshot=design, preview=True, inference_steps=30)
        await session.start()
        await session.send_text("你好，这是没有参考音频的文字设计候选。")
        await session.finish()
        _ = [chunk async for chunk in session.audio_chunks()]
        assert requests[0].url.path == "/v1/tasks/run"
        payload = json.loads(requests[0].content)["request"]
        assert payload["text"] == "(沉稳自然)你好，这是没有参考音频的文字设计候选。"
        assert payload["options"]["num_inference_steps"] == "30"
        assert payload["options"]["voxcpm2.full_decode"] == "true"
        assert "continuation_id" not in payload
        assert "voice_ref" not in payload
        assert "reference_text" not in payload
        assert "voice" not in payload and "style" not in payload
        assert client._warmed is False
        assert session.request_metrics[0]["engine_wall_ms"] == 1234.5
        assert session.request_metrics[0]["engine_audio_duration_ms"] == 1600.0
        assert session.request_metrics[0]["engine_rtf"] == 0.7715625

    asyncio.run(scenario())


def test_emotion_off_omits_dynamic_voxcpm2_instruction_and_invalid_labels_default_to_calm():
    async def run(emotion, emotion_mode="agent"):
        requests = []

        async def handler(request):
            requests.append(request)
            return _response(_audio_event(b"\x01\x00" * 8),
                             _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler, native_sample_rate=48000)
        client.emotion_mode = emotion_mode
        client.resolve_voice = lambda selected, preview=False: ResolvedLocalTtsVoice(
            model="voxcpm2", native_model_id="voxcpm2-stream", voice_id="voice-id",
            revision=1, reference_path=Path("voice2.wav").resolve(), reference_text="准确的参考转写。",
            style_description="温暖自然", emotion=client.normalize_emotion(selected), is_preview=preview,
        )
        session = client.session(emotion=emotion)
        await session.start()
        await session.send_text("测试情绪控制是否只影响模型提示。")
        await session.finish()
        _ = [chunk async for chunk in session.audio_chunks()]
        return json.loads(requests[0].content)["input"]

    assert asyncio.run(run("invalid")) == "测试情绪控制是否只影响模型提示。"
    assert asyncio.run(run("happy", "off")) == "测试情绪控制是否只影响模型提示。"


@pytest.mark.parametrize("emotion_mode,style,emotion,expected", [
    ("agent", "", "happy", True),
    ("off", "温暖自然", None, True),
    ("off", "", None, True),
])
def test_voxcpm2_formal_clone_uses_continuation_without_control(emotion_mode, style, emotion, expected):
    """验证旧情绪和风格不会阻断同一回答内续接，保留原音色和上下文释放。"""
    async def scenario():
        bodies, releases = [], []
        async def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/release"):
                releases.append(body)
                return httpx.Response(200, json={"released": True})
            bodies.append(body)
            return _response(_audio_event(b"\x01\x00" * 9), _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler, native_sample_rate=48000)
        client.emotion_mode = emotion_mode
        client.server.capabilities = {"acoustic_continuation": True}
        voice = replace(client.resolve_voice(emotion), style_description=style)
        session = client.session(voice_snapshot=voice)
        await session.start()
        for text in ("第一句完整正文。", "第二句完整正文。"):
            await session.send_text(text)
        await session.finish()
        assert len([chunk async for chunk in session.audio_chunks()]) == 2
        await session.close()
        assert all(("continuation_id" in body) == expected for body in bodies)
        assert all(body["options"]["num_inference_steps"] == 20 for body in bodies)
        assert [body["input"] for body in bodies] == ["第一句完整正文。", "第二句完整正文。"]
        assert bodies[0]["continuation_id"] == bodies[1]["continuation_id"]
        assert bodies[0]["voice_ref"] == bodies[1]["voice_ref"]
        assert len(releases) == int(expected)
        assert all(metrics["continuation_enabled"] == expected for metrics in session.request_metrics)
        assert session._segment_receipts[1].text_chars == len("第二句完整正文。")
        await client.close()
    asyncio.run(scenario())


def test_local_emotion_diagnostics_distinguish_prompt_request_from_unsupported_model():
    async def scenario():
        v1 = _fake_client(lambda request: _response())
        assert await v1.prepare_emotion("happy") == "calm"
        assert v1.emotion_status == "disabled"
        assert v1.emotion_transport == "reference_audio"
        v2 = _fake_client(lambda request: _response(), native_sample_rate=48000)
        assert await v2.prepare_emotion("happy") == "calm"
        assert v2.emotion_status == "disabled"
        assert v2.emotion_transport == "reference_audio"
        v2.emotion_mode = "off"
        assert await v2.prepare_emotion("happy") == "calm"
        assert v2.emotion_status == "disabled"
        await v1.close()
        await v2.close()
    asyncio.run(scenario())


def test_steps_hot_update_is_frozen_per_answer_and_new_answer_has_new_context():
    async def scenario():
        bodies = []
        async def handler(request):
            if request.url.path.endswith('/release'):
                return httpx.Response(200, json={"released": True})
            bodies.append(json.loads(request.content))
            return _response(_audio_event(b'\x01\x00' * 9), _event('speech.audio.done'), 'data: [DONE]\n\n')
        client = _fake_client(handler, native_sample_rate=48000)
        client.server.capabilities = {"acoustic_continuation": True}
        first = client.session()
        await first.start()
        await first.send_text('第一句完整正文。')
        client.inference_steps_by_model = {"voxcpm1": 12, "voxcpm2": 24}
        await first.send_text('第二句完整正文。')
        await first.finish()
        _ = [chunk async for chunk in first.audio_chunks()]
        await first.close()
        second = client.session()
        await second.start()
        await second.send_text('下一回答的新正文。')
        await second.finish()
        _ = [chunk async for chunk in second.audio_chunks()]
        await second.close()
        assert [body['options']['num_inference_steps'] for body in bodies] == [20, 20, 24]
        assert all(body['prompt_audio'] == body['voice_ref'] for body in bodies)
        assert all('voxcpm2.full_decode' not in body['options'] for body in bodies)
        assert bodies[0]['continuation_id'] == bodies[1]['continuation_id'] != bodies[2]['continuation_id']
        await client.close()
    asyncio.run(scenario())


def test_old_native_runtime_cannot_silently_ignore_high_fidelity_recipe():
    async def scenario():
        bodies = []
        async def handler(request):
            bodies.append(request)
            return _response()
        client = _fake_client(handler, native_sample_rate=48000)
        client.server.supports_reference_prompt = False
        session = client.session()
        await session.start()
        with pytest.raises(AudioCppTtsError, match="更新本地语音运行组件"):
            await session.send_text("必须使用准确参考转写。")
        assert not bodies
        await session.close()
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("preview,error_type,expected_requests,confirmed", [
    (True, "server_busy", 3, True), (False, "server_busy", 3, True),
    (True, "insufficient_memory", 1, False),
    (False, "insufficient_memory", 1, False),
])
def test_cancel_waits_for_native_release_without_retrying_unrelated_errors(preview, error_type, expected_requests, confirmed):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if len(calls) < 3:
                return httpx.Response(503, json={"error": {"type": error_type, "message": "测试忙碌"}})
            return httpx.Response(200, json={"released": True})
        client = _fake_client(handler, native_sample_rate=48000)
        session = client.session(preview=preview)
        session._context_requested = True
        await session.cancel()
        await session.close()
        assert len(calls) == expected_requests
        assert session.context_release_confirmed is confirmed
        await client.close()
    asyncio.run(scenario())


def test_native_release_busy_deadline_keeps_handoff_unconfirmed():
    """释放期限耗尽时保留未确认状态，不无限等待，也不重新提交正文。"""
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            return httpx.Response(503, json={"error": {"type": "server_busy"}})
        client = _fake_client(handler)
        client.timeout_seconds = 0
        session = client.session()
        session._context_requested = True
        await session.cancel()
        await session.close()
        assert calls == []
        assert session.context_release_confirmed is False
        assert session._context_requested is True
        await client.close()
    asyncio.run(scenario())


def test_formal_handoff_has_short_budget_shared_by_repeated_close(monkeypatch):
    """正式原生请求无需续接标记也会交接；默认合成超时不扩大释放预算。"""
    import voice_terminal.providers.audio_cpp_tts as provider
    async def scenario():
        calls = []
        clock = [100.0]
        async def handler(request):
            calls.append(request.extensions["timeout"]["read"])
            clock[0] += 2.9
            return httpx.Response(503, json={"error": {"type": "server_busy"}})
        client = _fake_client(handler)
        server = object.__new__(AudioCppTtsServer)
        server.__dict__.update(client.server.__dict__)
        client.server = server
        session = client.session()
        session._native_request_submitted = True
        assert not session._context_requested and client.timeout_seconds == 300
        monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        await session.cancel()
        await session.close()
        assert len(calls) == 4
        assert calls[:3] == [3.0] * 3 and 0 < calls[-1] < 1.31
        assert session._context_release_deadline == 110.0
        assert session.context_release_confirmed is False
        assert session._native_request_submitted
        await session.close()
        assert len(calls) == 4
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("description", ["温暖清楚", ""])
def test_preview_separates_creative_reference_from_transcript_cloning(tmp_path, description):
    async def scenario():
        bodies = []
        async def handler(request):
            bodies.append(json.loads(request.content))
            wav = io.BytesIO()
            with wave.open(wav, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b'\x01\x00' * 9)
            return httpx.Response(200, json={"audio": base64.b64encode(wav.getvalue()).decode("ascii")})
        client = _fake_client(handler, native_sample_rate=48000)
        client.server.capabilities = {"acoustic_continuation": True}
        reference = tmp_path / "reference.wav"
        reference.write_bytes(b"reference-audio")
        voice = replace(client.resolve_voice(None), reference_path=reference,
                        style_description=description, is_preview=True)
        session = client.session(voice_snapshot=voice, preview=True)
        await session.start()
        await session.send_text('生成可控克隆的目标样音。')
        await session.finish()
        _ = [chunk async for chunk in session.audio_chunks()]
        await session.close()
        request = bodies[0]['request']
        assert request['text'] == (f'({description})' if description else '') + '生成可控克隆的目标样音。'
        assert request['voice_ref'] == str(voice.reference_path)
        # 创作描述与音频续写提示互斥；纯克隆试音仍使用完整参考配方。
        assert 'continuation_id' not in request
        if description:
            assert 'reference_text' not in request and 'audio_base64' not in request
        else:
            assert request['reference_text'] == voice.reference_text
            assert base64.b64decode(request['audio_base64']) == reference.read_bytes()
        await client.close()
    asyncio.run(scenario())


def test_cancel_discards_queued_audio_and_next_session_stays_isolated():
    async def scenario():
        stalled_started = asyncio.Event()
        stalled_release = asyncio.Event()
        new_audio = b"\x05\x00" * 5

        class StalledStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _audio_event(b"\x09\x00" * 4).encode("utf-8")
                stalled_started.set()
                await stalled_release.wait()

            async def aclose(self):
                stalled_release.set()

        request_count = 0

        async def handler(request):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return httpx.Response(200, stream=StalledStream())
            return _response(_audio_event(new_audio), _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler)
        first = client.session()
        await first.start()
        sender = asyncio.create_task(first.send_text("旧事务正在合成"))
        await asyncio.wait_for(stalled_started.wait(), timeout=2)
        await first.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        assert [chunk async for chunk in first.audio_chunks()] == []

        second = client.session()
        await second.start()
        await second.send_text("新的事务独立播放")
        await second.finish()
        assert b"".join([chunk async for chunk in second.audio_chunks()]) == new_audio

    asyncio.run(scenario())


def test_segment_receipts_track_each_request_boundary_and_errors_wake_waiters():
    async def scenario():
        request_count = 0

        async def handler(_request):
            nonlocal request_count
            request_count += 1
            if request_count == 3:
                return _response(_event("error", message="synthetic failure"))
            pcm = bytes([request_count, 0]) * request_count
            return _response(_audio_event(pcm), _event("speech.audio.done"), "data: [DONE]\n\n")

        session = _fake_client(handler).session()
        await session.start()
        await session.send_text("第一段完整文本")
        await session.send_text("第二段完整文本")
        first = await session.wait_for_segment(0)
        second = await session.wait_for_segment(1)
        assert (first.index, first.text_chars, first.end_received_bytes) == (0, 7, 2)
        assert (second.index, second.text_chars, second.end_received_bytes) == (1, 7, 6)

        failing = asyncio.create_task(session.send_text("第三段触发错误"))
        with pytest.raises(AudioCppTtsError, match="合成失败"):
            await failing
        with pytest.raises(AudioCppTtsError, match="合成失败"):
            await session.wait_for_segment(2)

    asyncio.run(scenario())


def test_prewarm_synthesizes_then_discards_pcm():
    async def scenario():
        requested_texts = []

        async def handler(request):
            requested_texts.append(json.loads(request.content)["input"])
            return _response(_audio_event(b"\x00\x00" * 16), _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler)
        result = await client.prewarm(turn_id="turn-1")
        assert result["status"] == "READY"
        assert result["audio_bytes"] == 32
        assert requested_texts == ["你好，准备开始语音播报。"]
        assert client._warmed
        await client.close()
        assert not client._warmed
        client.server.close.assert_awaited_once()

    asyncio.run(scenario())


def test_server_health_checks_model_manifest_and_startup_owns_only_its_process():
    async def scenario():
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.gguf"
            voice = root / "reference.wav"
            model.write_bytes(b"model")
            voice.write_bytes(b"wav")
            config_path = root / "config" / "server.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({
                "host": "127.0.0.1",
                "port": 18881,
                "models": [{
                    "id": "voxcpm1-stream",
                    "path": str(model),
                    "default_voice_preset": {"voice_ref": str(voice)},
                }],
            }), encoding="utf-8")

            server = AudioCppTtsServer(root, config_path)
            server._http_transport = httpx.MockTransport(lambda request: httpx.Response(
                200,
                json=(
                    {"state": "ready"} if request.url.path == "/health" else
                    {"object": "list", "data": [{"id": "voxcpm1-stream", "loaded": True}]}
                ),
            ))
            assert await server._service_ready()
            management = server._management_http
            await server.ensure_ready()
            await server.ensure_ready()
            assert server._management_http is management

            server._http_transport = httpx.MockTransport(lambda request: httpx.Response(
                200,
                json=(
                    {"state": "ready"} if request.url.path == "/health" else
                    {"object": "list", "data": [{"id": "different-model"}]}
                ),
            ))
            assert not await server._service_ready()
            assert management.is_closed

            server._service_ready = AsyncMock(side_effect=[False, False, True])
            server._start_process = MagicMock()
            server._load_model_locked = AsyncMock()
            await server.ensure_ready(timeout_seconds=1)
            server._start_process.assert_called_once()
            server._load_model_locked.assert_awaited_once_with("voxcpm1")

    asyncio.run(scenario())


def test_runtime_server_config_registers_both_models_with_one_resident_slot():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        model_root = root / "models" / "audiocpp" / "tts"
        for relative in (
            Path("VoxCPM1-GGUF") / "voxcpm-0.5b-q8_0-audiovae-f16.gguf",
            Path("VoxCPM2-GGUF") / "voxcpm2-q8_0.gguf",
        ):
            model_path = model_root / relative
            model_path.parent.mkdir(parents=True, exist_ok=True)
            model_path.write_bytes(b"model")
        voice_path = root / "cache" / "reports" / "audio-cpp-tts-evaluation" / "reference.wav"
        voice_path.parent.mkdir(parents=True)
        voice_path.write_bytes(b"wav")
        config_path = root / "config" / "audiocpp" / "server.json"

        AudioCppTtsServer.write_runtime_config(root, config_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["max_loaded_models"] == 1
        assert config["lazy_load"] is True
        assert config["ui_management"] is True
        assert [model["id"] for model in config["models"]] == ["voxcpm1-stream", "voxcpm2-stream"]
        assert all(model["lazy"] for model in config["models"])
        assert all("default_voice_preset" not in model for model in config["models"])
        options = config["models"][1]["session_options"]
        assert options["voxcpm2.mem_saver"] is False
        assert options["voxcpm2.prompt_cache_slots"] == 1
        contexts = {name: size for name, size in options.items() if name.endswith("_context_mb")}
        assert len(contexts) == 9 and all(size == 128 for size in contexts.values())

        server = AudioCppTtsServer(root, config_path, local_model="voxcpm2")
        assert server.model_id == "voxcpm2-stream"
        assert server.native_sample_rate == 48000
        assert server.status_snapshot()["models"][1]["available"] is False  # 仅有权重，框架未就绪不能选择。


def test_server_switch_loads_selected_model_and_restores_old_model_on_failure():
    async def scenario():
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            voice = root / "reference.wav"
            voice.write_bytes(b"wav")
            models = []
            for model_id, family, name in (
                ("voxcpm1-stream", "voxcpm1", "one.gguf"),
                ("voxcpm2-stream", "voxcpm2", "two.gguf"),
            ):
                model = root / name
                model.write_bytes(b"model")
                models.append({
                    "id": model_id, "family": family, "path": str(model),
                    "task": "tts", "mode": "streaming",
                    "default_voice_preset": {"voice_ref": str(voice)},
                    "session_options": {f"{family}.prompt_cache_slots": 1},
                    "default_request_options": {"retry_badcase": False},
                })
            config_path = root / "server.json"
            config_path.write_text(json.dumps({"host": "127.0.0.1", "port": 18881, "models": models}), encoding="utf-8")
            server = AudioCppTtsServer(root, config_path)
            loaded = "voxcpm1-stream"
            fail_model_id = None
            actions = []

            async def handler(request):
                nonlocal loaded
                if request.url.path == "/health":
                    return httpx.Response(200, json={"state": "ready"})
                if request.url.path == "/v1/models":
                    return httpx.Response(200, json={
                        "data": [
                            {"id": "voxcpm1-stream", "loaded": loaded == "voxcpm1-stream"},
                            {"id": "voxcpm2-stream", "loaded": loaded == "voxcpm2-stream"},
                        ],
                    })
                payload = json.loads(request.content)
                actions.append((request.url.path, payload["id"]))
                if request.url.path.endswith("/unload"):
                    loaded = None
                    return httpx.Response(200, json={"loaded": False})
                if fail_model_id == payload["id"]:
                    return httpx.Response(500, text="synthetic model load failure")
                # 动态加载与失败回滚都必须保留会话选项、音色及请求默认值。
                expected = next(model for model in models if model["id"] == payload["id"])
                assert payload["session_options"] == expected["session_options"]
                assert "default_voice_preset" not in payload
                assert payload["default_request_options"] == expected["default_request_options"]
                loaded = payload["id"]
                return httpx.Response(200, json={"loaded": True})

            server._http_transport = httpx.MockTransport(handler)
            server._ready = True
            server._owned_loaded_model = "voxcpm1"  # 本用例模拟当前客户端已加载旧模型。
            await server.switch_model("voxcpm2")
            assert server.local_model == "voxcpm2"
            assert server.status_snapshot()["loaded_model"] == "voxcpm2"
            assert actions[:2] == [
                ("/v1/models/unload", "voxcpm1-stream"),
                ("/v1/models/load", "voxcpm2-stream"),
            ]

            fail_model_id = "voxcpm1-stream"
            with pytest.raises(AudioCppTtsError, match="已恢复 VoxCPM2"):
                await server.switch_model("voxcpm1")
            assert server.local_model == "voxcpm2"
            assert server.status_snapshot()["loaded_model"] == "voxcpm2"
            assert actions[-3:] == [
                ("/v1/models/unload", "voxcpm2-stream"),
                ("/v1/models/load", "voxcpm1-stream"),
                ("/v1/models/load", "voxcpm2-stream"),
            ]

            # 加载已执行但确认响应失败，仍须卸载本轮模型后恢复旧选择。
            fail_model_id = None
            read_entries = server._read_model_entries
            confirmation_failed = False

            async def read_with_one_confirmation_failure():
                nonlocal confirmation_failed
                entries = await read_entries()
                if entries["voxcpm1-stream"]["loaded"] and not confirmation_failed:
                    confirmation_failed = True
                    raise AudioCppTtsError("模拟模型驻留确认失败")
                return entries

            server._read_model_entries = read_with_one_confirmation_failure
            with pytest.raises(AudioCppTtsError, match="已恢复 VoxCPM2"):
                await server.switch_model("voxcpm1")
            assert server.local_model == "voxcpm2"
            assert actions[-4:] == [
                ("/v1/models/unload", "voxcpm2-stream"),
                ("/v1/models/load", "voxcpm1-stream"),
                ("/v1/models/unload", "voxcpm1-stream"),
                ("/v1/models/load", "voxcpm2-stream"),
            ]

    asyncio.run(scenario())


def test_server_missing_model_is_rejected_before_unloading_selected_model():
    async def scenario():
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_model = root / "one.gguf"
            first_model.write_bytes(b"model")
            missing_model = root / "missing.gguf"
            voice = root / "reference.wav"
            voice.write_bytes(b"wav")
            config_path = root / "server.json"
            config_path.write_text(json.dumps({
                "host": "127.0.0.1", "port": 18881,
                "models": [
                    {"id": "voxcpm1-stream", "path": str(first_model), "default_voice_preset": {"voice_ref": str(voice)}},
                    {"id": "voxcpm2-stream", "path": str(missing_model), "default_voice_preset": {"voice_ref": str(voice)}},
                ],
            }), encoding="utf-8")
            server = AudioCppTtsServer(root, config_path)
            server._ready = True
            server._loaded_model = "voxcpm1"
            server._post_model_action = AsyncMock()
            with pytest.raises(AudioCppTtsError, match="VoxCPM2.*缺失"):
                await server.switch_model("voxcpm2")
            assert server.local_model == "voxcpm1"
            assert server._loaded_model == "voxcpm1"
            server._post_model_action.assert_not_awaited()

    asyncio.run(scenario())


def test_model_switch_does_not_modify_borrowed_external_models():
    async def scenario():
        server = AudioCppTtsServer.__new__(AudioCppTtsServer)
        server._lock = asyncio.Lock()
        server.local_model = "voxcpm1"
        server._loaded_model = "voxcpm1"
        server._owned_loaded_model = None
        server._owns_process = False
        server._ready = True
        server._validate_model_assets = MagicMock()
        server._service_ready = AsyncMock(return_value=True)
        server._read_model_entries = AsyncMock(return_value={
            "voxcpm1-stream": {"loaded": True}, "voxcpm2-stream": {"loaded": True},
        })
        server._unload_model_locked = AsyncMock()
        server._load_model_locked = AsyncMock()
        with pytest.raises(AudioCppTtsError, match="外部客户端"):
            await server.switch_model("voxcpm2")
        server._unload_model_locked.assert_not_awaited()
        server._load_model_locked.assert_not_awaited()
        assert server.local_model == "voxcpm1"
        assert server._loaded_model == "voxcpm1"

    asyncio.run(scenario())


def test_server_close_only_unloads_models_loaded_by_this_client():
    from unittest.mock import AsyncMock

    async def scenario(owned):
        server = AudioCppTtsServer.__new__(AudioCppTtsServer)
        server._lock = asyncio.Lock()
        server._process = None
        server._owns_process = False
        server._loaded_model = "voxcpm2"
        server._owned_loaded_model = "voxcpm2" if owned else None
        server._management_http = None
        server._log_pump_thread = None
        server._log_file = None
        server._unload_model_locked = AsyncMock()
        await server.close()
        if owned:
            server._unload_model_locked.assert_awaited_once_with("voxcpm2-stream")
        else:
            server._unload_model_locked.assert_not_awaited()
        assert server._loaded_model is None

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_server_close_terminates_only_owned_audio_cpp_process():
    async def close_owned():
        server = AudioCppTtsServer.__new__(AudioCppTtsServer)
        server._lock = asyncio.Lock()
        server._ready = True
        server._process = MagicMock()
        server._process.poll.return_value = None
        server._process.wait.return_value = 0
        server._owns_process = True
        server._log_file = None
        server._log_pump_thread = None
        server._log_pump_done = None
        server._management_http = SimpleNamespace(aclose=AsyncMock())
        management = server._management_http
        process = server._process

        await server.close()

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        assert server._process is None
        assert server._owns_process is False
        management.aclose.assert_awaited_once()
        assert server._management_http is None

    async def close_borrowed():
        server = AudioCppTtsServer.__new__(AudioCppTtsServer)
        server._lock = asyncio.Lock()
        server._ready = True
        server._process = MagicMock()
        server._process.poll.return_value = None
        server._owns_process = False
        server._log_file = None
        server._log_pump_thread = None
        server._log_pump_done = None
        server._management_http = None

        process = server._process
        await server.close()

        process.terminate.assert_not_called()
        assert server._process is None

    asyncio.run(close_owned())
    asyncio.run(close_borrowed())


def test_session_runtime_builds_local_backend_without_reading_minimax_key():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        local_root = root / ".local"
        model = local_root / "model.gguf"
        voice = local_root / "reference.wav"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"model")
        voice.write_bytes(b"wav")
        config_path = local_root / "config" / "audiocpp" / "server-tts-evaluation.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": 18881,
            "models": [{
                "id": "voxcpm1-stream",
                "path": str(model),
                "default_voice_preset": {"voice_ref": str(voice)},
            }],
        }), encoding="utf-8")
        config = AppConfig.from_mapping(
            {"tts": {"backend": "voxcpm1", "local_model": "voxcpm1", "sample_rate": 16000}},
            base_dir=root,
        )
        product = ProductSettingsStore(path=local_root / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=product, runtime_data_dir=local_root / "data")

        def unexpected_key_lookup(_environment_name):
            raise AssertionError("VoxCPM1 模式不应读取 MiniMax API 密钥")

        runtime._api_key = unexpected_key_lookup
        remote, local = runtime._build_tts_clients()
        try:
            assert remote is None
            assert isinstance(local, AudioCppTtsClient)
            assert local.sample_rate == 16000
            assert config.tts.backend == "voxcpm1"
            assert runtime.product_settings.effective_config().tts.backend == "voxcpm1"
        finally:
            asyncio.run(local.close())


def test_packaged_runtime_uses_install_root_for_local_tts():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        program_root = root / "app"
        install_root = root / ".local"
        data_root = install_root / "data"
        model = install_root / "models" / "voxcpm1.gguf"
        voice = install_root / "cache" / "reference.wav"
        model.parent.mkdir(parents=True)
        voice.parent.mkdir(parents=True)
        model.write_bytes(b"model")
        voice.write_bytes(b"wav")
        config_path = install_root / "config" / "audiocpp" / "server-tts-evaluation.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": 18881,
            "models": [{
                "id": "voxcpm1-stream",
                "path": str(model),
                "default_voice_preset": {"voice_ref": str(voice)},
            }],
        }), encoding="utf-8")
        config = AppConfig.from_mapping(
            {"tts": {"backend": "voxcpm1", "sample_rate": 16000}},
            base_dir=program_root,
        )
        paths = AppPaths(program_root=program_root, install_root=install_root, data_root=data_root)
        config = replace(config, paths=paths)
        product = ProductSettingsStore(path=data_root / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=product)

        assert runtime._runtime_local_root == install_root.resolve()
        remote, local = runtime._build_tts_clients()
        try:
            assert remote is None
            assert isinstance(local, AudioCppTtsClient)
            assert local.server.config_path == config_path.resolve()
        finally:
            asyncio.run(local.close())


def test_runtime_migrates_legacy_default_voice_before_rewriting_native_config():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        local_root = root / ".local"
        data_root = local_root / "data"
        legacy_voice = local_root / "cache" / "reports" / "audio-cpp-tts-evaluation" / "reference.wav"
        legacy_voice.parent.mkdir(parents=True)
        samples = struct.pack("<" + "h" * (16000 * 5), *[
            int(4000 * ((index % 80) - 40) / 40) for index in range(16000 * 5)
        ])
        stream = io.BytesIO()
        with wave.open(stream, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(samples)
        legacy_voice.write_bytes(stream.getvalue())
        native_config = local_root / "config" / "audiocpp" / "server-tts-evaluation.json"
        native_config.parent.mkdir(parents=True)
        native_config.write_text(json.dumps({
            "host": "127.0.0.1", "port": 18881,
            "models": [{
                "id": "voxcpm1-stream", "path": str(local_root / "models" / "model.gguf"),
                "default_voice_preset": {
                    "voice_ref": str(legacy_voice), "reference_text": "原配置中的真实参考转写文本。",
                },
            }],
        }), encoding="utf-8")
        config = AppConfig.from_mapping({"tts": {"backend": "voxcpm1", "sample_rate": 16000}}, base_dir=root)
        store = ProductSettingsStore(path=data_root / "config" / "product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=data_root)
        try:
            _remote, local = runtime._build_tts_clients()
            voices = runtime.voice_library.for_model("voxcpm1").list_voices()
            self_voice = voices[0]
            assert len(voices) == 1
            assert self_voice["source"] == "legacy"
            assert self_voice["reference_text"] == "原配置中的真实参考转写文本。"
            selections = store.settings_only()["tts"]["active_voice_by_model"]
            assert selections["voxcpm1"] == {
                "voice_id": self_voice["id"], "revision": 1,
            }
            assert store.settings_only()["tts"]["backend"] == "voxcpm1"
            rewritten = json.loads(native_config.read_text(encoding="utf-8"))
            assert all("default_voice_preset" not in model for model in rewritten["models"])
            _remote, local_again = runtime._build_tts_clients()
            assert len(runtime.voice_library.for_model("voxcpm1").list_voices()) == 1
            assert len(runtime.voice_library.for_model("voxcpm2").list_voices()) == 1
            asyncio.run(local.close())
            asyncio.run(local_again.close())
        finally:
            runtime.tts = None


def test_voxcpm2_formal_clone_sends_reference_transcript():
    """二代正式克隆必须带上参考转写：原生用它走官方最高相似度配方。

    只发 voice_ref 时走 isolated 参考通道（实测相似度 0.6381）；带上与参考音频
    内容一致的 reference_text 后升到 0.8557。没有转写时不能凭空编一个。
    """
    async def scenario():
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            return _response(_audio_event(b"\x01\x00" * 9), _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler, native_sample_rate=48000)
        voice = client.resolve_voice(None)
        assert voice.reference_text, "夹具参考音色应带转写"

        session = client.session(voice_snapshot=voice)
        await session.start()
        await session.send_text("第一句完整正文。")
        await session.finish()
        _ = [chunk async for chunk in session.audio_chunks()]
        await session.close()
        assert bodies[0]["voice_ref"] == str(voice.reference_path.resolve())
        assert bodies[0]["reference_text"] == voice.reference_text

        assert bodies[0]["prompt_audio"] == bodies[0]["voice_ref"]
        # 旧的空转写音色保留存档，但不能静默退回低保真播报。
        blank = replace(voice, reference_text="   ")
        session = client.session(voice_snapshot=blank)
        await session.start()
        with pytest.raises(AudioCppTtsError, match="准确转写"):
            await session.send_text("第二句完整正文。")
        await session.close()
        assert len(bodies) == 1
        await client.close()

    asyncio.run(scenario())


def test_router_close_keeps_local_service_until_runtime_shutdown():
    async def scenario():
        remote = SimpleNamespace(close=AsyncMock())
        local = SimpleNamespace(close=AsyncMock())
        router = TtsBackendRouter(remote=remote, local=local, backend="voxcpm1")
        await router.close()
        local.close.assert_not_awaited()
        await router.shutdown()
        local.close.assert_awaited_once()

    asyncio.run(scenario())


def test_local_tts_player_rate_follows_model_native_output():
    """本地下行率必须等于模型原生率：48 kHz 原生降到 16 kHz 会丢掉 8 kHz 以上全部带宽。"""
    from voice_terminal.config import LOCAL_TTS_NATIVE_RATE, local_tts_native_rate, resolve_tts_sample_rate

    assert LOCAL_TTS_NATIVE_RATE == {"voxcpm1": 16000, "voxcpm2": 48000}
    assert local_tts_native_rate("voxcpm2") == 48000
    assert local_tts_native_rate("voxcpm1") == 16000
    # 未知模型取最保守值，不能凭配置漂移到更低带宽。
    assert local_tts_native_rate("unknown") == 16000

    # 本地后端忽略配置值，在线后端沿用配置值。
    assert resolve_tts_sample_rate("voxcpm1", "voxcpm2", 16000) == 48000
    assert resolve_tts_sample_rate("voxcpm1", "voxcpm1", 48000) == 16000
    assert resolve_tts_sample_rate("minimax", "voxcpm2", 32000) == 32000


def test_local_tts_config_defaults_to_native_rate_per_model():
    """启动配置：本地 VoxCPM2 缺省 48 kHz，VoxCPM1 缺省 16 kHz，在线缺省 32 kHz。"""
    config = AppConfig.from_mapping({"tts": {"backend": "voxcpm1"}}, base_dir=Path("."))
    assert config.tts.sample_rate == 48000  # local_model 缺省 voxcpm2。
    assert config.tts.online_sample_rate == 32000

    config = AppConfig.from_mapping(
        {"tts": {"backend": "voxcpm1", "local_model": "voxcpm1"}}, base_dir=Path("."))
    assert config.tts.sample_rate == 16000

    config = AppConfig.from_mapping({"tts": {"backend": "minimax"}}, base_dir=Path("."))
    assert config.tts.sample_rate == 32000

    # 本地后端即使显式写低下行率，也不能把 48 kHz 原生砍掉。
    config = AppConfig.from_mapping(
        {"tts": {"backend": "voxcpm1", "local_model": "voxcpm2", "sample_rate": 16000}},
        base_dir=Path("."))
    assert config.tts.sample_rate == 48000
    assert config.tts.online_sample_rate == 16000


def test_router_exposes_playback_reserve_from_active_client():
    """本地起播储备为零时，路由仍必须保留独立的欠载恢复策略。"""
    local = SimpleNamespace(
        playback_startup_buffer_ms=AudioCppTtsClient.playback_startup_buffer_ms,
        playback_resume_buffer_ms=AudioCppTtsClient.playback_resume_buffer_ms,
    )
    remote = SimpleNamespace()
    router = TtsBackendRouter(remote=remote, local=local, backend="voxcpm1")
    assert router.playback_startup_buffer_ms == 0
    assert router.playback_resume_buffer_ms == 400

    router.backend = "minimax"
    assert router.playback_startup_buffer_ms == 0
    assert router.playback_resume_buffer_ms == 0


def test_native_rate_multi_segment_streaming_keeps_transport_and_receipts():
    """48 kHz 下行不得破坏流式 TTS：SSE 入口、分块投递、分段回执与续接全部保留。"""
    async def scenario():
        paths, bodies = [], []

        async def handler(request):
            if request.url.path.endswith("/contexts/release"):
                return httpx.Response(200, json={"released": True})
            body = request.content
            if not body:
                # 会话 start 会先探活原生服务（同样复用本 handler）。
                return httpx.Response(200, json={"status": "ok", "data": []})
            paths.append(request.url.path)
            bodies.append(json.loads(body))
            return _response(_audio_event(b"\x01\x00" * 6),
                             _event("speech.audio.done"), "data: [DONE]\n\n")

        client = _fake_client(handler, native_sample_rate=48000)
        client.server.capabilities = {"acoustic_continuation": True}
        assert client.sample_rate == 48000
        session = client.session(voice_snapshot=client.resolve_voice(None))
        assert session._resampler is None, "原生率下行不重采样"
        await session.start()
        # 多段：流式仍然逐段提交、逐段收音频、逐段落回执。
        for text in ("第一句完整正文。", "第二句完整正文。", "第三句完整正文。"):
            await session.send_text(text)
        await session.finish()
        chunks = [chunk async for chunk in session.audio_chunks()]
        await session.close()

        assert len(chunks) == 3, "每个文本分段各收一份音频"
        assert all(path.endswith("/v1/audio/speech") for path in paths), "流式仍走 SSE 入口"
        assert [body["input"] for body in bodies] == [
            "第一句完整正文。", "第二句完整正文。", "第三句完整正文。"]
        # 音色参考保持不变；续接仅在同一事务内共享上下文。
        assert len({body["voice_ref"] for body in bodies}) == 1
        assert len({body["continuation_id"] for body in bodies}) == 1
        assert [metric["segment_index"] for metric in session.request_metrics] == [0, 1, 2]
        assert all(metric["native_pcm_bytes"] == metric["pcm_bytes"]
                   for metric in session.request_metrics), "48 kHz 直通：原生字节等于下行字节"
        assert session.completed_segments == 3
        await client.close()

    asyncio.run(scenario())
