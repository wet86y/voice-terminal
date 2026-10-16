"""通用提前合成与声学上下文隔离；不启动音频设备。"""
import asyncio
import json
from types import SimpleNamespace

import httpx

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.tts.capabilities import TtsCapabilities, capabilities_of
from voice_terminal.controller.speech_admission import AdmissionState
from tests.test_audio_cpp_tts import _fake_client, _response, _audio_event, _event
from tests.test_sentence_scheduling import _sentence_output_harness


def test_player_reclock_changes_rate_without_replacing_instance():
    """换下行采样率必须原地置换：控制器/事务输出/语音管线都缓存了播放器引用。

    替换实例会让它们继续指向已停止的旧对象（音频入旧队列、进度读旧计数），
    所以 reclock 只重开设备流，实例身份保持不变。
    """
    player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
    marker = player  # 模拟被控制器缓存的引用。
    assert player.running is False, "无设备环境下播放器不应已启动"
    assert player.reclock(48000) is True
    assert player is marker, "实例不能被替换，否则缓存引用全部失效"
    assert player.sample_rate == 48000
    assert player.running is False or player.sample_rate == 48000
    # 同率重复置换是空操作，非法率被拒绝且不改变状态。
    assert player.reclock(48000) is False
    assert player.reclock(0) is False
    assert player.sample_rate == 48000


def test_player_reclock_drops_stale_bytes_when_stream_running(monkeypatch):
    """运行中的播放器换时钟：先清空按旧时钟采集的队列，再重开设备流。"""
    player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
    marker = player
    calls: list[str] = []
    monkeypatch.setattr(player, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(player, "start", lambda: calls.append("start"))
    player._stream = object()  # 模拟已启动的设备流。
    assert player.reclock(48000) is True
    assert calls == ["stop", "start"], "必须按 stop → start 顺序重开设备流"
    assert player is marker and player.sample_rate == 48000


def test_stream_buffer_avoids_fragmented_playback_and_drains_short_tail():
    player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
    player.allow("local")
    player.configure_stream_buffer("local", startup_ms=100, resume_ms=60)
    player.enqueue("local", b"\x01\x00" * 40)
    block = bytearray(40)
    player._callback(block, 20, None, None)
    assert block == bytes(40) and player.buffered_ms == 40
    player.enqueue("local", b"\x02\x00" * 60)
    player._callback(block, 20, None, None)
    assert block == b"\x01\x00" * 20
    for _ in range(4):
        player._callback(block, 20, None, None)
    player._callback(block, 20, None, None)
    assert block == bytes(40)
    player.enqueue("local", b"\x03\x00" * 20)
    player._callback(block, 20, None, None)
    assert block == bytes(40) and player.buffered_ms == 20
    player.finish_stream("local")
    player._callback(block, 20, None, None)
    assert block == b"\x03\x00" * 20 and player.buffered_ms == 0
    assert player.diagnostics["underrun_ms"] == 40
    player.allow("next")
    player.enqueue("next", b"\x04\x00" * 20)
    player._callback(block, 20, None, None)
    assert block == b"\x04\x00" * 20


def test_local_resampling_keeps_existing_player_clock():
    import numpy as np
    import soxr
    from voice_terminal.providers.audio_cpp_tts import _Pcm16Resampler
    source = (np.sin(np.arange(4800) * .1) * 10000).astype("int16")
    resampler = _Pcm16Resampler(48000, 32000)
    actual = resampler.push(source[:1800].tobytes()) + resampler.push(source[1800:].tobytes()) + resampler.finish()
    expected = soxr.resample(source, 48000, 32000, quality="HQ").tobytes()
    # 两次独立int16（16位整数）转换各有取整/抖动，差值最多两个最低有效位。
    assert len(actual) == len(expected) == 6400
    difference = np.frombuffer(actual, dtype="<i2").astype(np.int32) - np.frombuffer(expected, dtype="<i2")
    assert np.abs(difference).max() <= 2
    assert np.sqrt(np.mean(difference.astype(float) ** 2)) < 1


def test_short_clause_preserves_normal_lock_window():
    state = AdmissionState(lead_ms=0, sentence_window=True)
    state.push("好的，")
    assert state.pop_sentence_block(min_effective_chars=10) is None
    assert state.pop_sentence_block(min_effective_chars=10, allow_short=True) == "好的，"


def test_generic_provider_synthesizes_during_hold_but_device_stays_silent():
    async def scenario():
        output, handle, session, _ = _sentence_output_harness()
        client = output.tts
        client.capabilities = TtsCapabilities()
        handle.tts_client = client
        handle.playback_ready.clear()
        handle.text = "这是足够十个有效字符的完整短句。"
        output.runtime.user_speaking = True
        output._playback_hold = True
        output._playback_release.clear()
        player = output.player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        producer_finish = asyncio.Event()

        async def deltas():
            yield "这是足够十个有效字符的完整短句。"
            await producer_finish.wait()
            yield None

        task = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(), handle=handle,
            speech_id=handle.speech_id,
        ))
        handle.task = task
        try:
            async with asyncio.timeout(2):
                while not session.received_bytes:
                    await asyncio.sleep(.005)
            assert session.sent == ["这是足够十个有效字符的完整短句。"]
            assert not task.done()
            session.audio._queue[0] = b"\x01\x00" * 3000
            buffer = bytearray(100)
            player._callback(buffer, 50, None, None)
            assert buffer == bytes(100)
            assert player.buffered_ms == 0
            output.runtime.user_speaking = False
            assert output.release_held() == handle.speech_id
            async with asyncio.timeout(2):
                while player.buffered_ms == 0:
                    await asyncio.sleep(.005)
            assert player.buffered_ms == 3000
            player._callback(buffer, 50, None, None)
            assert player.has_started(handle.workspace.playback_key)
            assert buffer == b"\x01\x00" * 50
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_new_speech_ticket_isolated_from_old_same_request():
    async def scenario():
        output, old, _, _ = _sentence_output_harness()
        boundary = output.inference_boundary
        async with boundary.request(old.identity):
            assert not boundary._allowed(old)
            new = SimpleNamespace(identity=old.identity, speech_id="new-speech")
            boundary.authorize_new(new)
            assert boundary._allowed(new)
            assert not boundary._allowed(old)
            async with boundary.request(old.identity):
                assert not boundary._allowed(new)
        assert not boundary._requests and not boundary._new_speech
    asyncio.run(scenario())


def test_pause_active_speech_stops_production_without_revoking_locked_playback():
    async def scenario():
        output, handle, _, _ = _sentence_output_harness()
        handle.task = asyncio.create_task(asyncio.Event().wait())
        handle.workspace.playback_started = True
        try:
            assert output.suspend_pending() == handle.speech_id
            assert not handle.production_ready.is_set()
            assert handle.playback_ready.is_set()
            output.release_held()
            assert not handle.production_ready.is_set()
            output.resume_pending()
            assert handle.production_ready.is_set() and handle.state == "READY"
        finally:
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
    asyncio.run(scenario())


def test_remote_retry_creates_fresh_session_instead_of_returning_failed_one():
    async def scenario():
        from unittest.mock import MagicMock
        output, handle, old, _ = _sentence_output_harness()
        replacement = SimpleNamespace()
        handle.tts_session = old
        handle.tts_client = SimpleNamespace(session=MagicMock(return_value=replacement))
        assert output._new_tts_session(handle.workspace, fresh=True) is replacement
        assert handle.tts_client.session.call_args.kwargs["fresh"] is True
    asyncio.run(scenario())


def test_formal_local_retry_freezes_voice_steps_and_transaction_lease(tmp_path):
    async def scenario():
        from voice_terminal.tts.voices import ModelVoiceLibraries
        from tests.test_tts_voices import _wav_bytes, _create_voice
        output, _, _, _ = _sentence_output_harness()
        client = _fake_client(lambda request: _response())
        del client.resolve_voice  # 使用正式解析方法，替代协议夹具的固定音色。
        libraries = client.voice_library = ModelVoiceLibraries(tmp_path)
        library = libraries.for_model("voxcpm1")
        library.register_default(_wav_bytes(), "默认转写。")
        asset, voice = _create_voice(library)
        library.activate({"voice_id": voice["id"], "revision": 1})
        client.active_voice_provider = lambda model: libraries.for_model(model).active_selection
        output.tts = client
        output.handle = None
        finished = asyncio.Event()
        async def run(handle):
            await finished.wait()
        output._run = run
        handle = await output.begin(("episode", "frozen"), "frozen-turn")
        original = output._new_tts_session(handle.workspace)
        library.update_voice(voice["id"], 1, {**voice, "name": "热更新名称"})
        client.inference_steps_by_model["voxcpm1"] = 15
        await original.close()
        retried = output._new_tts_session(handle.workspace, fresh=True)
        assert retried.voice_snapshot.revision == 1
        assert retried._inference_steps == 10
        await retried.close()
        library.delete_voice(voice["id"])
        library.collect_garbage(max_age_seconds=-1)
        assert library.asset_path(asset["asset_id"]).is_file()
        finished.set()
        await handle.task
        await asyncio.sleep(0)
        if output.tasks:
            await asyncio.gather(*list(output.tasks), return_exceptions=True)
        library.collect_garbage(max_age_seconds=-1)
        assert not original.voice_snapshot.reference_path.exists()
        await client.close()
    asyncio.run(scenario())


def test_continuation_context_is_transaction_owned_and_released():
    async def scenario():
        bodies, released = [], []
        async def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/release"):
                released.append(body["continuation_id"])
                return httpx.Response(200, json={"released": True})
            bodies.append(body)
            return _response(_audio_event(b"\x01\x00"*20), _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler)
        client.server.capabilities = {"acoustic_continuation": True}
        for texts in (("第一句。", "第二句。"), ("新事务。",)):
            session = client.session()
            await session.start()
            for text in texts:
                await session.send_text(text)
            await session.finish()
            assert len([chunk async for chunk in session.audio_chunks()]) == len(texts)
            await session.close()
        assert bodies[0]["continuation_id"] == bodies[1]["continuation_id"]
        assert bodies[2]["continuation_id"] != bodies[1]["continuation_id"]
        assert released == [bodies[0]["continuation_id"], bodies[2]["continuation_id"]]
        await client.close()
    asyncio.run(scenario())


def test_continuation_falls_back_once_before_audio_only():
    async def scenario():
        calls = []
        async def handler(request):
            if request.url.path.endswith("/release"):
                return httpx.Response(200, json={"released": True})
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                return httpx.Response(500, json={"error": "invalid context"})
            return _response(_audio_event(b"\x01\x00"*5), _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler)
        client.server.capabilities = {"acoustic_continuation": True}
        session = client.session()
        await session.start()
        await session.send_text("新尾巴。")
        await session.finish()
        assert b"".join([pcm async for pcm in session.audio_chunks()]) == b"\x01\x00"*5
        assert len(calls) == 2
        await session.close()
        await client.close()
    asyncio.run(scenario())


def test_partial_audio_failure_never_replays_the_segment():
    async def scenario():
        calls = []
        async def handler(request):
            if request.url.path.endswith("/release"):
                return httpx.Response(200, json={"released": True})
            calls.append(request)
            return _response(_audio_event(b"\x01\x00"*5), _event("error", message="decode failed"))
        client = _fake_client(handler)
        client.server.capabilities = {"acoustic_continuation": True}
        session = client.session()
        await session.start()
        import pytest
        with pytest.raises(Exception):
            await session.send_text("不会重复播放。")
        assert len(calls) == 1
        await session.close()
        await client.close()
    asyncio.run(scenario())


def test_legacy_optimization_cannot_disable_generic_synthesis(tmp_path):
    from voice_terminal.config import AppConfig
    from voice_terminal.server.product import ProductSettingsStore
    store = ProductSettingsStore(path=tmp_path/"settings.json", config=AppConfig())
    backend = store.effective_config().tts.backend
    store.save_tts_local_model({"local_optimization": False})
    assert store.effective_config().tts.backend == backend
    assert store.effective_config().tts.local_optimization is True


def test_idle_prewarm_is_bounded_context_free_and_real_request_preempts():
    async def scenario():
        warm_entered = asyncio.Event()
        requests = []
        async def handler(request):
            if request.url.path.endswith("/release"):
                return httpx.Response(200, json={"released": True})
            body = json.loads(request.content)
            requests.append(body)
            if body["options"].get("max_tokens") == 8:
                warm_entered.set()
                await asyncio.Event().wait()
            return _response(_audio_event(b"\x01\x00"*5), _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler)
        client.server.capabilities = {"acoustic_continuation": True}
        warm = asyncio.create_task(client.prewarm())
        await asyncio.wait_for(warm_entered.wait(), 1)
        session = client.session()
        await session.start()
        await asyncio.wait_for(session.send_text("真实请求优先。"), 1)
        assert warm.cancelled()
        assert "continuation_id" not in requests[0]
        assert requests[1]["options"]["max_tokens"] > 8, "正式回答使用独立预算，不能沿用预热的八令牌上限"
        assert client._real_waiters == 0
        assert client._warm_task is None
        assert (await client.prewarm())["reason"] == "already_warmed"
        await session.close()
        await client.close()
    asyncio.run(scenario())


def test_held_audio_limit_fails_without_blocking_and_cancel_discards_all():
    async def scenario():
        from voice_terminal.providers.audio_cpp_tts import AudioCppTtsError
        import pytest
        client = _fake_client(lambda request: _response())
        session = client.session()
        await session.start()
        await session._queue_pcm(b"\x01\x00"*10)
        session._buffered_bytes = 32*1024*1024
        with pytest.raises(AudioCppTtsError, match="32 MiB"):
            await asyncio.wait_for(session._queue_pcm(b"\x02\x00"), 1)
        await session.cancel()
        assert session._buffered_bytes == 0
        assert [pcm async for pcm in session.audio_chunks()] == []
        await session.close()
        await client.close()
    asyncio.run(scenario())
