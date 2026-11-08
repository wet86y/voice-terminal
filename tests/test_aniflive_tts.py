"""AnifLive（本地语音服务）：真实预热生命周期、正式优先及独立首块计时契约。"""


import asyncio


import logging


import time


from types import SimpleNamespace


from unittest.mock import AsyncMock


import httpx


import pytest


from voice_terminal.controller.realtime_controller import RealtimeController


from voice_terminal.controller.turn_workspace import TurnWorkspace


from voice_terminal.providers.aniflive_tts import AnifLiveTtsClient, AnifLiveTtsError


from voice_terminal.tts.router import TtsBackendRouter


class SpeechService:
    def __init__(self, *, stall=False, fail=False, odd_gate=None):
        self.sessions = {}
        self.requests = []
        self.stall = stall
        self.fail = fail
        self.audio_started = asyncio.Event()
        self.odd_gate = odd_gate

    async def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"ready": True, "model": "test-model", "voice": "default"})
        if path == "/v1/sessions":
            sid = str(len(self.sessions) + 1)
            self.sessions[sid] = SimpleNamespace(submitted=asyncio.Event(), cancelled=False, flushed=False, state="open")
            return httpx.Response(201, json={"id": sid})
        sid = path.split("/")[3]
        session = self.sessions[sid]
        if path.endswith("/audio"):
            service = self

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    await session.submitted.wait()
                    service.audio_started.set()
                    if service.stall:
                        await asyncio.Event().wait()
                    yield b"\x01"
                    if service.odd_gate is not None:
                        await service.odd_gate.wait()
                    yield b"\x00\x02\x00"
                    session.state = "closed"

            return httpx.Response(200, stream=Stream(), headers={"X-TTS-Sample-Rate": "32000"})
        if path.endswith("/segments"):
            session.submitted.set()
            return httpx.Response(201, json={})
        if path.endswith("/flush"):
            session.flushed = True
            return httpx.Response(200, json={})
        if path.endswith("/cancel"):
            session.cancelled = True
            session.state = "cancelled"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"state": session.state,
            "segments": [{"state": "failed" if self.fail else "completed", "error": "failed" if self.fail else None}]})

    def client(self, **kwargs):
        client = AnifLiveTtsClient(model_id="test-model", **{"gpu_max_performance": False, **kwargs})
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return client


def test_prewarm_drains_a_private_completed_segment_and_never_reuses_it():
    async def run():
        service = SpeechService()
        client = service.client()
        result = await client.prewarm(turn_id="turn-a")
        assert result["status"] == "READY"
        assert service.sessions["1"].flushed and service.sessions["1"].state == "closed"
        assert not client._foreground_sessions
        session = client.session(turn_id="turn-a")
        await session.start()
        assert session.session_id == "2"
        assert (await client.prewarm(turn_id="turn-a"))["reason"] == "formal_session_active"
        await client.close()
        assert service.sessions["2"].cancelled
    asyncio.run(run())


def test_cold_reference_preparation_does_not_consume_inference_prewarm_budget(monkeypatch):
    from voice_terminal.providers.aniflive_tts import AnifLiveTtsSession
    original = AnifLiveTtsSession.start
    async def cold_start(session):
        await asyncio.sleep(.05)  # 模拟模型／多情绪参考准备，超过静默推理预算。
        await original(session)
    monkeypatch.setattr(AnifLiveTtsSession, 'start', cold_start)
    async def run():
        client = SpeechService().client(prewarm_timeout_seconds=.025)
        try:
            assert (await client.prewarm())['status'] == 'READY'
        finally:
            await client.close()
    asyncio.run(run())


def test_prewarm_can_be_disabled_independently_of_the_performance_option():
    async def run():
        service = SpeechService()
        client = service.client(prewarm_enabled=False)
        router = TtsBackendRouter(remote=None, local=client, backend="aniflive")
        assert not router.supports_prewarm and not router.vad_prewarm
        assert (await client.prewarm())["reason"] == "local_optimization_disabled"
        assert not service.requests
        await client.close()
    asyncio.run(run())


def test_default_and_performance_policies_are_mutually_exclusive():
    default = AnifLiveTtsClient()
    assert not default.gpu_performance.enabled
    assert default.prewarm_enabled and default.vad_prewarm
    performance = AnifLiveTtsClient(gpu_max_performance=True)
    assert not performance.prewarm_enabled and not performance.capabilities.idle_prewarm


def test_hot_performance_toggle_cancels_warmup_and_restores_event_prewarm():
    async def run():
        service = SpeechService(stall=True)
        client = service.client()
        applied, restored = [], []
        client.gpu_performance.apply = lambda: applied.append(True)
        client.gpu_performance.restore = lambda: restored.append(True)
        router = TtsBackendRouter(remote=None, local=client, backend="aniflive")
        warm = asyncio.create_task(client.prewarm(turn_id="test-turn"))
        await asyncio.wait_for(service.audio_started.wait(), 1)
        await router.configure_optimization(gpu_max_performance=True, local_optimization=True)
        await asyncio.gather(warm, return_exceptions=True)
        assert service.sessions["1"].cancelled
        assert not router.supports_prewarm and not router.vad_prewarm
        assert not router.capabilities.idle_prewarm
        assert applied and client.local_optimization
        count = len(service.requests)
        assert (await client.prewarm())["reason"] == "gpu_max_performance_enabled"
        assert len(service.requests) == count
        await router.configure_optimization(gpu_max_performance=False, local_optimization=True)
        assert restored and router.supports_prewarm and router.vad_prewarm
        assert router.capabilities.idle_prewarm
        service.stall = False
        assert (await client.prewarm())["status"] == "READY"
        await client.close()
    asyncio.run(run())


def test_formal_session_preempts_stalled_prewarm_using_only_its_own_cancel():
    async def run():
        service = SpeechService(stall=True)
        client = service.client()
        warm = asyncio.create_task(client.prewarm(turn_id="old-turn"))
        await asyncio.wait_for(service.audio_started.wait(), 1)
        await client.discard_prewarm(turn_id="unrelated-turn")
        assert not warm.done()
        session = client.session(turn_id="new-turn")
        await asyncio.wait_for(session.start(), 1)
        assert service.sessions["1"].cancelled
        assert session.session_id == "2" and not service.sessions["2"].cancelled
        assert ("POST", "/v1/audio/cancel") not in service.requests
        await asyncio.gather(warm, return_exceptions=True)
        await client.close()
        assert client._prewarm_task is None and not client._foreground_sessions
    asyncio.run(run())


def test_prewarm_timeout_or_failed_segment_is_not_reported_ready():
    async def run():
        for service, error in ((SpeechService(stall=True), TimeoutError), (SpeechService(fail=True), AnifLiveTtsError)):
            client = service.client(prewarm_timeout_seconds=.03 if service.stall else 3.0)
            with pytest.raises(error):
                await client.prewarm(turn_id="turn")
            assert client._prewarm_task is None
            if service.stall:
                assert service.sessions["1"].cancelled
            await client.close()
    asyncio.run(run())


def test_network_first_pcm_is_recorded_before_a_held_consumer_and_samples_align(caplog):
    async def run():
        service = SpeechService()
        client = service.client()
        session = client.session(turn_id="held-turn")
        await session.start()
        await session.send_text("不应出现在诊断日志里的正文")
        await session.finish()
        await asyncio.wait_for(session._receiver, 1)
        assert session.first_audio_ready_at is not None
        assert not session._consumer_started
        assert session._audio_queue.qsize() <= 3
        assert b"".join([block async for block in session.audio_chunks()]) == b"\x01\x00\x02\x00"
        await client.close()
    with caplog.at_level(logging.INFO, logger="voice_terminal.providers.aniflive_tts"):
        asyncio.run(run())
    assert "first_pcm_timing" in caplog.text and "first_pcm_consumed" in caplog.text
    assert "不应出现在诊断日志里的正文" not in caplog.text


def test_finished_input_is_still_cancelled_when_audio_has_not_ended():
    async def run():
        service = SpeechService(stall=True)
        client = service.client()
        session = client.session()
        await session.start()
        await session.send_text("测试")
        await session.finish()
        await session.close()
        assert service.sessions["1"].cancelled
        assert not client._foreground_sessions
        await client.close()
    asyncio.run(run())


def test_vad_edges_coalesce_inflight_work_and_refresh_after_completion():
    async def run():
        service = SpeechService()
        client = service.client()
        router = TtsBackendRouter(remote=None, local=client, backend="aniflive")
        c = RealtimeController.__new__(RealtimeController)
        c.tts = router
        c.workspace = ws = TurnWorkspace("vad-turn")
        c._tts_prewarm_tasks = {}
        c._track = lambda task: None
        async def publish(*args, **kwargs):
            pass
        c._publish_tts_prewarm = publish
        c._refresh_vad_tts_prewarm(ws, trigger="asr_start")
        first = c._tts_prewarm_tasks[ws.turn_id]
        c._refresh_vad_tts_prewarm(ws, trigger="vad_start")
        assert c._tts_prewarm_tasks[ws.turn_id] is first
        await first
        assert ws.tts_prewarm_status == "READY"
        c._refresh_vad_tts_prewarm(ws, trigger="vad_end")
        await c._tts_prewarm_tasks[ws.turn_id]
        assert len(service.sessions) == 2
        # 切换后依然能清理旧的本地预热任务。
        await router.discard_prewarm(turn_id=ws.turn_id)
        await router.shutdown()
    asyncio.run(run())


def test_performance_request_is_forwarded_to_status_and_restored_on_close():
    async def run():
        service = SpeechService()
        client = service.client()
        actions = []
        client.gpu_performance = SimpleNamespace(apply=lambda: actions.append("apply"),
            restore=lambda: actions.append("restore"), status=lambda: {"applied": True})
        await client.ensure_ready()
        assert client.model_status()["gpu_performance"]["applied"] is True
        await client.close()
        assert actions == ["apply", "restore"]
    asyncio.run(run())


def test_a_held_audio_queue_is_bounded_and_close_cancels_the_blocked_receiver():
    async def run():
        service = SpeechService()
        async def handle(request):
            if request.url.path.endswith("/audio"):
                class LargeStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        await service.sessions["1"].submitted.wait()
                        for _ in range(20):
                            yield b"\x01\x00" * 32768
                return httpx.Response(200, stream=LargeStream())
            return await service.handle(request)
        client = service.client()
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        session = client.session()
        await session.start()
        await session.send_text("播放仍未放行")
        for _ in range(10):
            await asyncio.sleep(0)
        assert session._audio_queue.qsize() == 3
        receiver = session._receiver
        assert not receiver.done()
        await asyncio.wait_for(client.close(), 1)
        assert receiver.done() and service.sessions["1"].cancelled
        assert not client._foreground_sessions
    asyncio.run(run())


def test_cancelling_close_during_remote_cancel_still_reaps_receiver_and_lease():
    async def run():
        service = SpeechService(stall=True)
        entered = asyncio.Event()
        async def handle(request):
            if request.url.path.endswith("/cancel"):
                entered.set()
                await asyncio.Event().wait()
            return await service.handle(request)
        client = service.client()
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        session = client.session()
        await session.start()
        await session.send_text("关闭过程中取消")
        receiver = session._receiver
        close = asyncio.create_task(session.close())
        await asyncio.wait_for(entered.wait(), 1)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert receiver.done() and session._response is None
        assert not client._foreground_sessions
        await client.close()
    asyncio.run(run())


def test_switching_away_during_prewarm_releases_the_original_backend_lease():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def warm(**kwargs):
            started.set()
            await release.wait()
            return {"status": "READY"}
        local = SimpleNamespace(ensure_ready=AsyncMock(), close=AsyncMock(), prewarm=warm)
        router = TtsBackendRouter(remote=SimpleNamespace(), local=local, backend="aniflive")
        pending = asyncio.create_task(router.prewarm())
        await started.wait()
        await router.switch("minimax")
        release.set()
        await pending
        for _ in range(3):
            await asyncio.sleep(0)
        assert router.active_local_sessions == 0
        local.close.assert_awaited_once()
    asyncio.run(run())


def test_partial_audio_followed_by_server_failure_cannot_finish_successfully():
    async def run():
        client = SpeechService(fail=True).client()
        session = client.session()
        await session.start()
        await session.send_text('服务端在部分音频后失败。')
        await session.finish()
        returned = bytearray()
        with pytest.raises(AnifLiveTtsError, match='未成功完成'):
            async for chunk in session.audio_chunks():
                returned.extend(chunk)
        assert returned == b'\x01\x00\x02\x00'
        assert not session.server_finished and session.completed_segments == 0
        await client.close()
    asyncio.run(run())


def test_wait_and_chunk_options_are_frozen_while_receiving_continues():
    async def run():
        client = SpeechService().client(aniflive_first_audio_wait_ms=120)
        session = client.session()
        await client.configure_optimization(gpu_max_performance=False, local_optimization=True,
            aniflive_chunk_optimization=False, aniflive_first_audio_wait_ms=0)
        assert session.chunk_optimization is True and session.first_audio_wait_ms == 120
        assert client.session().chunk_optimization is False
        await session.start()
        await session.send_text('验证固定等待与独立接收。')
        await session.finish()
        await asyncio.wait_for(session._receiver, 1)
        arrived = session.first_audio_ready_at
        assert arrived is not None and session.audio_bytes_received == 4
        assert b''.join([chunk async for chunk in session.audio_chunks()]) == b'\x01\x00\x02\x00'
        assert time.perf_counter() - session.first_audio_perf_at >= .12
        await client.close()
    asyncio.run(run())


def test_first_network_time_is_recorded_before_an_odd_packet_can_form_pcm():
    async def run():
        gate = asyncio.Event()
        service = SpeechService(odd_gate=gate)
        client = service.client(aniflive_first_audio_wait_ms=120)
        session = client.session()
        try:
            await session.start(); await session.send_text('检查网络单字节分片。'); await session.finish()
            await asyncio.wait_for(service.audio_started.wait(), 1)
            await asyncio.sleep(0)
            first = session.first_audio_perf_at
            assert first is not None and session.audio_bytes_received == 0
            gate.set()
            assert b''.join([chunk async for chunk in session.audio_chunks()]) == b'\x01\x00\x02\x00'
            assert session.first_audio_perf_at == first and time.perf_counter() - first >= .12
        finally:
            gate.set()
            await client.close()
    asyncio.run(run())


def test_performance_preference_does_not_mutate_admitted_answer_snapshot():
    async def run():
        client = SpeechService().client()
        applied, restored = [], []
        client.gpu_performance.apply = lambda: applied.append(client.gpu_performance.enabled)
        client.gpu_performance.restore = lambda: restored.append(True)
        old = client.session()
        await client.configure_optimization(gpu_max_performance=True, local_optimization=True)
        assert not old.gpu_max_performance and not client.gpu_performance.enabled
        assert (await client.prewarm())['reason'] == 'gpu_max_performance_enabled'
        await old.start(); await old.send_text('保存前已受理的回答。'); await old.finish()
        assert applied[-1] is False
        assert b''.join([chunk async for chunk in old.audio_chunks()])
        await old.close()
        assert client.gpu_performance.enabled
        following = client.session()
        await client.configure_optimization(gpu_max_performance=False, local_optimization=True)
        assert following.gpu_max_performance and client.gpu_performance.enabled
        await following.start(); await following.send_text('第二次保存前已受理的回答。'); await following.finish()
        assert applied[-1] is True
        assert b''.join([chunk async for chunk in following.audio_chunks()])
        await following.close()
        assert not client.gpu_performance.enabled and restored
        await client.close()
    asyncio.run(run())


def test_real_speech_pipeline_keeps_network_first_time_when_playback_is_held():
    from tests.test_speech_admission import _output_harness
    async def run():
        service = SpeechService()
        client = service.client()
        output, handle, tts, player = _output_harness("direct", 0)
        tts.transport = "http_pcm"
        tts.session_instance = session = client.session(turn_id=handle.turn_id)
        handle.playback_ready.clear()
        handle.text = "检查播放等待与网络生成分别计时。"
        handle.finished = True
        async def deltas():
            yield handle.text
        task = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(), speech_id=handle.speech_id, handle=handle))
        try:
            await asyncio.wait_for(service.audio_started.wait(), 1)
            await asyncio.wait_for(session._receiver, 1)
            network_at = session.first_audio_ready_at
            assert network_at is not None and not player.enqueued
            await asyncio.sleep(.04)
            release_at = time.monotonic()
            handle.playback_ready.set()
            await asyncio.wait_for(task, 2)
            assert handle.workspace.timings["first_audio"] == network_at
            assert handle.workspace.timings["first_pcm"] >= release_at > network_at
            assert player.enqueued
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()
    asyncio.run(run())
