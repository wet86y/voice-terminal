"""通用提前合成与声学上下文隔离；不启动音频设备。"""
import asyncio
import json
import logging
import re
from types import SimpleNamespace

import httpx

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.gpu_performance import GpuSnapshot
from voice_terminal.tts.capabilities import TtsCapabilities, capabilities_of
from voice_terminal.controller.speech_admission import AdmissionState
from voice_terminal.controller.speech_pipeline import (
    safe_stream_reserve_ms,
    stream_reserve_clamp_reason,
)
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


# --- C／D／E：播放储备死锁护栏、自适应恢复门槛、每轮降级信号 -------------------


def _arm_local_stream_gate(output, *, startup_ms, resume_ms, cap_ms, prefetch_ms):
    """把语音管线切到本地储备闸门分支（provider 声明储备 + 队列上限 + 句窗门限）。"""

    client = output.tts
    client.playback_startup_buffer_ms = startup_ms
    client.playback_resume_buffer_ms = resume_ms
    client.sentence_prefetch_ms = prefetch_ms
    output.options.tts_local_buffer_ms = cap_ms
    return client


async def _wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())


async def _pump_callbacks(player, *, frames, until, timeout=3.0):
    """手动推进真实 PcmPlayer 的设备回调（不打开音频设备），直到 until() 成立。

    夹具采样率 1000 赫兹、单声道：一帧 = 1 毫秒，因此 frames 即本次回调消耗的毫秒数。
    """

    block = bytearray(frames * 2)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        player._callback(block, frames, None, None)
        if until():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.001)


def test_startup_reserve_is_clamped_below_cap_and_sentence_prefetch() -> None:
    """C：起播储备必须夹在入队上限与句窗门之下，否则播放永不开始。"""

    # provider 声明 8000 毫秒、队列上限 8000、句窗门限 2000 时以句窗门为准。
    assert safe_stream_reserve_ms(requested_ms=8000, cap_ms=8000, prefetch_ms=2000) == 1500
    # 队列上限更紧时以队列上限为准（现场默认 2000）。
    assert safe_stream_reserve_ms(requested_ms=8000, cap_ms=2000, prefetch_ms=3000) == 2000
    # 现场默认 800/2000/3000 一个字都不变：夹紧不改变正常路径。
    assert safe_stream_reserve_ms(requested_ms=800, cap_ms=2000, prefetch_ms=3000) == 800
    # 恢复门槛要留出队列上限间隔，且同样受句窗门约束。
    assert safe_stream_reserve_ms(
        requested_ms=2000, cap_ms=2000, prefetch_ms=3000, cap_margin_ms=200
    ) == 1800
    assert safe_stream_reserve_ms(
        requested_ms=2000, cap_ms=4000, prefetch_ms=2000, cap_margin_ms=200
    ) == 1500
    assert stream_reserve_clamp_reason(
        requested_ms=8000, cap_ms=8000, prefetch_ms=2000
    ) == "sentence_prefetch"
    assert stream_reserve_clamp_reason(
        requested_ms=8000, cap_ms=2000, prefetch_ms=3000
    ) == "queue_cap+sentence_prefetch"
    assert stream_reserve_clamp_reason(
        requested_ms=800, cap_ms=2000, prefetch_ms=3000
    ) == "none"


def test_local_startup_reserve_cannot_livelock_sentence_window() -> None:
    """C：startup > 句窗门限的危险配置下，首块音频仍必须能起播。

    现场默认 800 毫秒 < 首块约 2 秒，安全余量是隐式的。这里把 startup 提到 8000、
    句窗门限压到 2000，复现审计仿真 F 的活锁条件（120 秒内播放 0 毫秒）：
    首块 3000 毫秒入队后句窗门关闭，若起播门槛仍是 8000，播放永不开始。
    """

    async def scenario():
        output, handle, session, _ = _sentence_output_harness(min_effective_chars=1)
        _arm_local_stream_gate(output, startup_ms=8000, resume_ms=400, cap_ms=8000, prefetch_ms=2000)
        player = output.player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        key = handle.workspace.playback_key
        release = asyncio.Event()

        async def deltas():
            yield "第一句。"
            await release.wait()
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            assert await _wait_until(lambda: player.buffered_ms >= 3000), "首块 3000 毫秒必须先入队"
            # 起播储备被夹到 min(8000, 8000, 2000-500) = 1500 毫秒。
            assert player.stream_reserve_ms(key)["startup_ms"] == 1500
            # 句窗门此时是关着的：未播内容 3000 毫秒 > 门限 2000 毫秒。
            assert player.progress(key)["played_ms"] == 0
            assert await _pump_callbacks(player, frames=100, until=lambda: player.has_started(key), timeout=1)
            assert player.progress(key)["buffered_ms"] > 0, "起播必须靠首块音频，而不是靠整轮结束绕过闸门"
            release.set()
            assert await _pump_callbacks(player, frames=100, until=lambda: player.buffered_ms == 0)
            await asyncio.wait_for(running, 5)
            assert player.played_ms == 3000
            assert session.sent == ["第一句。"]
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_set_stream_reserve_changes_recovery_without_rearming_gate() -> None:
    """D：抬高恢复门槛只改门槛，不重新武装闸门、不清队列。"""

    player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
    player.allow("local")
    player.configure_stream_buffer("local", startup_ms=100, resume_ms=60)
    assert player.stream_reserve_ms("local") == {"armed": True, "startup_ms": 100, "resume_ms": 60}
    # 没有装配流式储备闸门的 key（在线后端路径）必须完全不受影响。
    assert player.set_stream_reserve("other", resume_ms=400) is False
    assert player.stream_reserve_ms("other") == {"armed": False, "startup_ms": 0, "resume_ms": 0}

    player.enqueue("local", b"\x01\x00" * 100)  # 100 毫秒
    block = bytearray(40)
    for _ in range(6):  # 播完并进入欠载（第 6 次回调开始静音）
        player._callback(block, 20, None, None)
    assert player.has_started("local")
    assert player.buffered_ms == 0
    assert player.diagnostics["underrun_episodes"] == 1
    assert player.diagnostics["underrun_ms"] == 20

    # 播放中途抬高门槛：不得把代次打回"等待起播"，也不得清空队列。
    assert player.set_stream_reserve("local", resume_ms=200) is True
    assert player.stream_reserve_ms("local")["resume_ms"] == 200
    assert player.has_started("local")
    player.enqueue("local", b"\x02\x00" * 150)  # 150 毫秒 < 200 毫秒新门槛
    player._callback(block, 20, None, None)
    assert block == bytes(40), "未攒够抬高的门槛前不得恢复播放"
    assert player.buffered_ms == 150
    assert player.diagnostics["underrun_episodes"] == 1, "同一次欠载片段不得重复计数"
    player.enqueue("local", b"\x03\x00" * 60)  # 合计 210 毫秒 ≥ 200 毫秒
    player._callback(block, 20, None, None)
    assert block == b"\x02\x00" * 20
    # 起播门槛同样可单独更新。
    assert player.set_stream_reserve("local", startup_ms=250) is True
    assert player.stream_reserve_ms("local")["startup_ms"] == 250


def test_adaptive_resume_only_raises_after_measured_rtf_or_underrun(caplog) -> None:
    """D：R≥1 时抬高恢复门槛；R<1 时一个字节都不改（零代价）。"""

    async def scenario(rtf, expected_resume):
        output, handle, session, _ = _sentence_output_harness(min_effective_chars=1)
        _arm_local_stream_gate(output, startup_ms=100, resume_ms=400, cap_ms=2000, prefetch_ms=3000)
        session.request_metrics = [{"rtf": rtf}]
        player = output.player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        key = handle.workspace.playback_key
        release = asyncio.Event()

        async def deltas():
            yield "第一句。"
            await release.wait()
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            assert await _wait_until(lambda: bool(session.receipts))
            assert await _wait_until(
                lambda: player.stream_reserve_ms(key)["resume_ms"] == expected_resume
            ), player.stream_reserve_ms(key)
            assert player.diagnostics["underrun_ms"] == 0
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    for rtf, expected_resume in ((1.40, 1800), (0.62, 400)):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="voice_terminal.controller.speech_pipeline"):
            asyncio.run(scenario(rtf, expected_resume))
        degraded = [r.getMessage() for r in caplog.records
                    if "controller.tts_playback_degraded" in r.getMessage()]
        if rtf >= 1:
            assert not degraded, "降级信号属于收尾汇总，不应在块回执处提前打印"
        else:
            assert degraded == [], "R<1 且无欠载时必须零代价，不得出现降级信号"


def test_degraded_turn_emits_single_warning_with_gpu_snapshot(caplog) -> None:
    """E：一条 WARNING 给出 turn_id／max_rtf／underrun／静音占比／resume／显卡快照。"""

    async def scenario(events):
        output, handle, session, _ = _sentence_output_harness(min_effective_chars=1)
        client = _arm_local_stream_gate(
            output, startup_ms=100, resume_ms=60, cap_ms=2000, prefetch_ms=3000
        )
        session.request_metrics = [{"rtf": 1.6}]

        class Gpu:
            def snapshot(self):
                return GpuSnapshot(clock_mhz=2767, sm_clock_mhz=2745,
                                   power_w=36.4, utilization_percent=41)

            def contention(self):
                # 现场取证口径：外部显卡进程 + HAGS 状态必须一并落盘，
                # 否则无法区分“外部程序抢显卡”与“原生推理本身变慢”。
                return {
                    "clock_mhz": 2767, "sm_clock_mhz": 2745,
                    "power_w": 36.4, "utilization_percent": 41,
                    "external_processes": [{"pid": 4242, "name": "game.exe", "kind": "graphics"}],
                    "own_processes": [{"pid": 4243, "name": "audiocpp_server.exe", "kind": "compute"}],
                    "scheduling": {"hwschmode": 2, "enabled": True, "note": "2=hardware-scheduling"},
                    "reason": None,
                }

        client.gpu_performance = Gpu()
        original_emit = output.runtime.emit

        async def collect(kind, payload):
            events.append((kind, payload))
            await original_emit(kind, payload)

        output.runtime.emit = collect
        player = output.player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        key = handle.workspace.playback_key
        release = asyncio.Event()

        async def deltas():
            yield "第一句。"
            await release.wait()
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            # 先按实测 RTF ≥ 1 抬高恢复门槛，再让队列在回调中干涸。
            assert await _wait_until(lambda: player.stream_reserve_ms(key)["resume_ms"] == 1800)
            assert await _pump_callbacks(player, frames=100, until=lambda: player.has_started(key), timeout=1)
            assert await _pump_callbacks(
                player, frames=100, timeout=5,
                until=lambda: player.diagnostics["underrun_ms"] > 0,
            ), "抬高的门槛必须真的把播放停住并计入欠载"
            release.set()
            assert await _pump_callbacks(player, frames=100, until=lambda: player.buffered_ms == 0)
            await asyncio.wait_for(running, 5)
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    events: list[tuple[str, dict]] = []
    with caplog.at_level(logging.WARNING, logger="voice_terminal.controller.speech_pipeline"):
        asyncio.run(scenario(events))

    lines = [r.getMessage() for r in caplog.records
             if "controller.tts_playback_degraded" in r.getMessage()]
    assert len(lines) == 1, lines
    line = lines[0]
    # 逐字段解析而非一条长正则：字段变多后长正则脆弱，且失败时看不出断点。
    def field(name: str) -> str:
        match = re.search(rf"(?<![\w.]){re.escape(name)}=(\S+)", line)
        assert match, f"缺少字段 {name}: {line}"
        return match.group(1)

    assert field("turn_id") == "turn"
    assert float(field("max_rtf")) == 1.6
    underrun_ms = int(field("underrun_ms"))
    played_ms = int(field("played_ms"))
    assert underrun_ms > 0 and played_ms > 0
    assert abs(float(field("silence_ratio")) - underrun_ms / (underrun_ms + played_ms)) < 0.001
    assert int(field("resume_ms")) == 1800
    assert int(field("startup_ms")) == 100
    assert int(field("gpu_external")) == 1
    assert int(field("gpu_own")) == 1
    assert field("gpu_hags") == "2"
    assert "game.exe" in line, line
    gpu = line[line.rindex("gpu=") + len("gpu="):]
    for value in (2767, 2745, 36.4, 41):
        assert str(value) in gpu, line
    details = [payload for kind, payload in events
               if kind == "speech.progress" and payload.get("event") == "tts.playback_degraded"]
    assert len(details) == 1, events
    assert details[0]["gpu"]["clock_mhz"] == 2767
    assert details[0]["gpu"]["external_processes"][0]["name"] == "game.exe"
    assert details[0]["resume_ms"] == 1800
    assert details[0]["silence_ratio"] > 0
