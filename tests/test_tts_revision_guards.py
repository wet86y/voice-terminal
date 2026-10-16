"""本地语音换率、有效参考配方及异常生成保护；不启动音频设备。"""
import asyncio
import base64
import json
import queue
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.audio.processing import AudioPreprocessor
from voice_terminal.config import AppConfig
from voice_terminal.providers.audio_cpp_tts import AudioCppDegenerateError, AudioCppTtsServer
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.tts.voices import VoiceLibrary
from tests.test_audio_cpp_tts import _audio_event, _event, _fake_client, _response
from tests.test_tts_voices import _wav_bytes
from tests.test_sentence_scheduling import _sentence_output_harness


def test_reverse_reclock_discards_old_queue_and_dequeued_generation():
    processor = AudioPreprocessor(sample_rate=16000, reverse_sample_rate=16000)
    frames = []
    processor._processor = SimpleNamespace(reset=Mock(), process_reverse_stream=frames.append)
    processor.status = "READY"
    processor._reverse_queue = queue.Queue()
    processor.push_reverse(b"\x01\x00" * 320)
    stale = processor._reverse_queue.get_nowait()
    processor.push_reverse(b"\x01\x00" * 320)
    processor._reverse_remainder.extend(b"\x01\x00")
    processor.reclock_reverse(48000)
    assert processor._reverse_queue.empty() and not processor._reverse_remainder
    assert processor._last_reverse_at is None
    # 模拟换率前已出队、换率后才被工作线程处理的旧代次。
    processor._reverse_queue.put(stale)
    processor.push_reverse(b"\x02\x00" * 960)  # 48 kHz 下 20 毫秒。
    processor._reverse_queue.put(None)
    processor._reverse_loop()
    assert frames == [b"\x02\x00" * 160] * 2  # 正好两个 10 毫秒参考帧。
    processor._processor.reset.assert_called_once()


@pytest.mark.parametrize("old,new", [(16000, 48000), (48000, 16000)])
def test_reclock_rebuilds_cue_and_synchronizes_reference_before_start(old, new):
    player = PcmPlayer(sample_rate=old, channels=1, output_device=None)
    order = []
    player._stream = object()
    player.stop = lambda: order.append("stop")
    player.start = lambda: order.append("start")
    player.reclock(new, on_rate_changed=lambda rate: order.append(rate))
    assert order == ["stop", new, "start"]
    assert len(player._wake_cue_pcm) == round(new * .18) * 2


@pytest.mark.parametrize("old_rate", [16000, 32000])
def test_remote_to_v2_changes_all_output_clocks_without_replacing_player(old_rate):
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = replace(AppConfig(), tts=replace(AppConfig().tts, sample_rate=old_rate))
        previous = runtime.tts = SimpleNamespace(shutdown=AsyncMock())
        player = runtime.player = PcmPlayer(sample_rate=old_rate, channels=1, output_device=None)
        player.start = Mock()
        runtime.audio_processor = AudioPreprocessor(reverse_sample_rate=old_rate)
        runtime._pending_local_tts_rate = None
        runtime.state = SimpleNamespace(current={})
        runtime.controller = None
        runtime._active_voice_snapshot = lambda model: None
        runtime._build_tts_clients = lambda: (None, SimpleNamespace(
            ensure_ready=AsyncMock(), sample_rate=runtime.config.tts.sample_rate))
        await runtime.switch_tts_selection("voxcpm1", "voxcpm2")
        assert runtime.player is player
        assert runtime.config.tts.sample_rate == runtime.tts.local.sample_rate == 48000
        assert player.sample_rate == runtime.audio_processor.reverse_sample_rate == 48000
        previous.shutdown.assert_awaited_once()
    asyncio.run(scenario())

@pytest.mark.parametrize("old_model,new_model,rate", [
    ("voxcpm1", "voxcpm2", 48000), ("voxcpm2", "voxcpm1", 16000),
])
def test_local_model_sync_keeps_player_and_reverse_clock_aligned(old_model, new_model, rate):
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = AppConfig.from_mapping({"tts": {"backend": "voxcpm1", "local_model": old_model}})
        old_rate = runtime.config.tts.sample_rate
        runtime.tts = SimpleNamespace(local=SimpleNamespace(sample_rate=old_rate))
        player = runtime.player = PcmPlayer(sample_rate=old_rate, channels=1, output_device=None)
        player.start = Mock()
        runtime.audio_processor = AudioPreprocessor(reverse_sample_rate=old_rate)
        runtime.state = SimpleNamespace(current={})
        await runtime._sync_local_tts_sample_rate(new_model)
        assert runtime.player is player
        assert runtime.config.tts.sample_rate == runtime.tts.local.sample_rate == rate
        assert player.sample_rate == runtime.audio_processor.reverse_sample_rate == rate
    asyncio.run(scenario())


@pytest.mark.parametrize("response_key", ["audio", "named_audio_outputs"])
def test_reference_preview_uses_prompt_audio_task_and_native_pcm(tmp_path, response_key):
    async def scenario():
        reference = tmp_path / "reference.wav"
        reference.write_bytes(_wav_bytes(sample_rate=16000, channels=1))
        wav = _wav_bytes(seconds=1, sample_rate=48000, channels=1)
        encoded = base64.b64encode(wav).decode("ascii")
        requests = []
        async def handler(request):
            requests.append(request)
            result = ({"audio": encoded} if response_key == "audio"
                      else {"named_audio_outputs": [{"name": "speech", "audio": encoded}]})
            return httpx.Response(200, json=result)
        client = _fake_client(handler, native_sample_rate=48000)
        voice = replace(client.resolve_voice(None), reference_path=reference, is_preview=True)
        session = client.session(voice_snapshot=voice, preview=True)
        await session.start()
        await session.send_text("测试最高还原度配方。")
        await session.finish()
        chunks = [pcm async for pcm in session.audio_chunks()]
        await session.close()
        assert requests[0].url.path == "/v1/tasks/run"
        body = json.loads(requests[0].content)["request"]
        assert base64.b64decode(body["audio_base64"]) == reference.read_bytes()
        assert body["reference_text"] == voice.reference_text
        assert body["voice_ref"] == str(reference)
        assert "continuation_id" not in body
        assert len(b"".join(chunks)) == 48000 * 2
        assert session.completed_segments == 1
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("native_rate", [16000, 48000])
def test_overlong_stream_is_rejected_cleared_and_released_without_replay(native_rate, caplog):
    async def scenario():
        submitted, released = [], []
        async def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/release"):
                released.append(body["continuation_id"])
                return httpx.Response(200, json={"released": True})
            submitted.append(body)
            return _response(_audio_event(b"\x01\x00" * native_rate),
                             _audio_event(b"\x02\x00" * native_rate * 14),
                             _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler, native_sample_rate=native_rate)
        client.server.capabilities = {"acoustic_continuation": True}
        session = client.session()
        rejected = session.audio_rejected_callback = Mock()
        await session.start()
        with pytest.raises(AudioCppDegenerateError):
            await session.send_text("我是七三，你的语音助手。")
        assert session.audio_rejected and session._buffered_bytes == 0
        assert session.completed_segments == 0 and session.request_metrics == []
        rejected.assert_called_once()
        assert len(submitted) == 1 and len(released) == 1
        assert 0 < submitted[0]["options"]["max_tokens"] < 1024
        with pytest.raises(AudioCppDegenerateError):
            _ = [pcm async for pcm in session.audio_chunks()]
        with pytest.raises(AudioCppDegenerateError):
            await session.send_text("坏段之后不能继续沿用上下文。")
        assert len(submitted) == 1
        assert "audio_cpp_tts.degenerate_segment" in caplog.text
        await session.close()
        await client.close()
    asyncio.run(scenario())


def test_high_fidelity_garbage_collection_removes_pair_and_legacy_orphans(tmp_path):
    library = VoiceLibrary(tmp_path / "voices", model="voxcpm2")
    asset = library.put_pcm(b"\x00\x10" * 16000, sample_rate=16000)
    library.put_high_fidelity_pcm(asset["asset_id"], b"\x00\x10" * 48000, sample_rate=48000)
    main, hifi = library.asset_path(asset["asset_id"]), library.high_fidelity_path(asset["asset_id"])
    orphan = library.assets_dir / ("a" * 32 + ".hifi.wav")
    orphan.write_bytes(b"legacy")
    library.collect_garbage(max_age_seconds=-1)
    assert not main.exists() and not hifi.exists() and not orphan.exists()


def test_degenerate_audio_stops_pipeline_and_reports_failure_without_replay():
    async def scenario():
        requests = []
        async def handler(request):
            if request.url.path.endswith("/release"):
                return httpx.Response(200, json={"released": True})
            requests.append(request)
            return _response(_audio_event(b"\x01\x00" * 16000 * 14),
                             _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler)
        output, handle, _, _ = _sentence_output_harness()
        player = output.player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
        handle.tts_client = client
        handle.tts_backend = "voxcpm1"
        handle.text = "我是七三，你的语音助手。"
        details = output._detail = AsyncMock()
        async def deltas():
            yield handle.text
            yield None
        await asyncio.wait_for(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(), handle=handle,
            speech_id=handle.speech_id), 3)
        assert player.buffered_ms == 0 and len(requests) == 1
        assert output.state.current["tts_status"] == "DEGRADED"
        assert any(call.kwargs.get("status") == "DEGRADED" and "时长预算" in call.kwargs.get("error", "")
                   for call in details.call_args_list)
        await client.close()
    asyncio.run(scenario())


def test_failed_device_reclock_restores_previous_output_clocks():
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = AppConfig()
        previous = runtime.tts = SimpleNamespace(shutdown=AsyncMock())
        runtime.controller = None
        runtime.state = SimpleNamespace(current={})
        runtime._pending_local_tts_rate = None
        runtime.audio_processor = AudioPreprocessor(reverse_sample_rate=32000)
        runtime.player = PcmPlayer(sample_rate=32000, channels=1, output_device=None)
        attempts = []
        def start():
            attempts.append(runtime.player.sample_rate)
            if runtime.player.sample_rate == 48000:
                raise RuntimeError("模拟设备拒绝 48 kHz")
        runtime.player.start = start
        runtime._active_voice_snapshot = lambda model: None
        local = SimpleNamespace(ensure_ready=AsyncMock(), close=AsyncMock())
        runtime._build_tts_clients = lambda: (None, local)
        with pytest.raises(RuntimeError, match="设备拒绝"):
            await runtime.switch_tts_selection("voxcpm1", "voxcpm2")
        assert runtime.tts is previous
        assert runtime.config.tts.sample_rate == runtime.player.sample_rate == 32000
        assert runtime.audio_processor.reverse_sample_rate == 32000
        assert attempts == [48000, 32000]
        local.close.assert_awaited_once()
    asyncio.run(scenario())


def test_local_model_device_failure_rolls_back_native_model_and_all_clocks():
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = AppConfig.from_mapping({"tts": {"backend": "voxcpm1", "local_model": "voxcpm1"}})
        local = SimpleNamespace(sample_rate=16000)
        runtime.tts = SimpleNamespace(local=local, switch_local_model=AsyncMock())
        runtime.player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
        runtime.audio_processor = AudioPreprocessor(reverse_sample_rate=16000)
        runtime._pending_local_tts_rate = None
        runtime.state = SimpleNamespace(current={})
        def start():
            if runtime.player.sample_rate == 48000:
                raise RuntimeError("模拟本地模型设备换率失败")
        runtime.player.start = start
        with pytest.raises(RuntimeError, match="设备换率失败"):
            await runtime.switch_tts_selection("voxcpm1", "voxcpm2")
        assert [call.args[0] for call in runtime.tts.switch_local_model.call_args_list] == ["voxcpm2", "voxcpm1"]
        assert runtime.config.tts.local_model == "voxcpm1"
        assert runtime.config.tts.sample_rate == local.sample_rate == 16000
        assert runtime.player.sample_rate == runtime.audio_processor.reverse_sample_rate == 16000
        assert runtime._local_tts_load_state == "ready"
    asyncio.run(scenario())


@pytest.mark.parametrize("backend,model,available,rate", [
    ("minimax", "voxcpm2", ["voxcpm2"], 48000),
    ("voxcpm1", "voxcpm1", ["voxcpm2"], 48000),
    ("voxcpm1", "voxcpm1", ["voxcpm1"], 16000),
])
def test_startup_selection_and_first_player_share_native_reverse_rate(tmp_path, monkeypatch, backend, model, available, rate):
    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime.config = replace(AppConfig(), tts=replace(AppConfig().tts, backend=backend, local_model=model))
    runtime._runtime_local_root = tmp_path
    runtime._api_key = lambda name: ""
    monkeypatch.setattr(AudioCppTtsServer, "available_models", lambda root: available)
    runtime._select_available_tts()
    assert runtime.config.tts.sample_rate == rate
    runtime.player = None
    runtime._pending_local_tts_rate = None
    runtime.audio_processor = AudioPreprocessor(reverse_sample_rate=32000)
    runtime.quiet_policy = SimpleNamespace(passive=lambda: False)
    runtime.audio_meter = SimpleNamespace(feed=Mock())
    runtime.state = SimpleNamespace(current={})
    def start(player):
        assert player.sample_rate == runtime.audio_processor.reverse_sample_rate == rate
    monkeypatch.setattr(PcmPlayer, "start", start)
    runtime._ensure_player()


@pytest.mark.parametrize("method,args", [
    ("switch_local_tts_model", ("voxcpm2",)),
    ("switch_tts_selection", ("voxcpm1", "voxcpm2")),
])
def test_queued_audio_rejects_switch_before_native_or_router_mutation(method, args):
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = AppConfig()
        runtime.player = SimpleNamespace(buffered_ms=100)
        runtime.tts = SimpleNamespace(switch_local_model=AsyncMock())
        runtime._build_tts_clients = Mock()
        with pytest.raises(RuntimeError, match="音频尚未排空"):
            await getattr(runtime, method)(*args)
        runtime.tts.switch_local_model.assert_not_awaited()
        runtime._build_tts_clients.assert_not_called()
        assert runtime.config.tts.backend == "minimax"
    asyncio.run(scenario())
