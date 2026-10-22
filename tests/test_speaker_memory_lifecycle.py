"""声纹临时实例：原生调用完成前保活，恢复或设置页测试后释放缓存。"""
from __future__ import annotations

import asyncio
import gc
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import weakref

import pytest

from voice_terminal.core.state import RuntimeState
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.speaker.client import SpeakerWorkerClient


def runtime_stub():
    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime.config = SimpleNamespace(
        speaker_model_path=Path("test-model.onnx"),
        speaker=SimpleNamespace(model_id="wespeaker-cnceleb-resnet34"),
        audio=SimpleNamespace(sample_rate=16000),
        compute=SimpleNamespace(devices={"campplus": "cpu"}, fallback_devices={}),
    )
    runtime._speaker_engine = None
    runtime._speaker_engine_lock = threading.RLock()
    return runtime


def test_healthy_sidecar_releases_the_cached_local_fallback():
    async def scenario():
        runtime = runtime_stub()
        runtime._speaker_worker_lock = asyncio.Lock()
        runtime._speaker_worker_process = None
        runtime.state = RuntimeState()
        runtime.state.set_service("speaker", "READY")
        runtime.speaker = SimpleNamespace(connected=True, degraded=False, health=AsyncMock())
        local_engine = SimpleNamespace(session=object())
        runtime._speaker_engine = local_engine
        assert await runtime.ensure_speaker_worker()
        assert runtime._speaker_engine is None
        # 清除缓存不强制销毁还可能被原生调用持有的推理会话。
        assert local_engine.session is not None
    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_settings_test_releases_local_model_on_success_and_failure(fails):
    runtime = runtime_stub()
    runtime._speaker_engine = SimpleNamespace(session=object())
    runtime._score_speaker_audio_owned = Mock(
        side_effect=RuntimeError("测试推理失败") if fails else None,
        return_value={"state": "confirmed"},
    )
    if fails:
        with pytest.raises(RuntimeError, match="测试推理失败"):
            runtime._score_speaker_audio(b"pcm", 1000)
    else:
        assert runtime._score_speaker_audio(b"pcm", 1000) == {"state": "confirmed"}
    assert runtime._speaker_engine is None


def test_cache_release_keeps_inflight_native_call_alive_until_completion():
    async def scenario():
        runtime = runtime_stub()
        started, finish = threading.Event(), threading.Event()
        instances = []

        class Engine:
            def __init__(self, model_path, **_):
                self.model_path = model_path
                self.loaded = False
                self.session = object()
                instances.append(weakref.ref(self))

            def load(self):
                self.loaded = True

        def native_call():
            engine = runtime._get_speaker_engine()
            started.set()
            if not finish.wait(timeout=5):
                raise TimeoutError("原生调用未获准完成")
            assert engine.session is not None
            return (1.0, 0.0)

        with patch("voice_terminal.server.speaker_runtime.create_speaker_engine",
                   side_effect=lambda _model_id, model_path, **kwargs: Engine(model_path, **kwargs)):
            task = asyncio.create_task(asyncio.to_thread(native_call))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                runtime._release_speaker_engine()
                gc.collect()
                assert runtime._speaker_engine is None
                assert instances[0]() is not None
            finally:
                finish.set()
            assert await task == (1.0, 0.0)
            await asyncio.sleep(0)
            gc.collect()
            assert instances[0]() is None
    asyncio.run(scenario())


def test_enrollment_rejection_does_not_keep_a_loaded_local_model():
    async def scenario():
        runtime = runtime_stub()
        runtime._speaker_engine = SimpleNamespace(session=object())
        runtime._speaker_captures = {}
        runtime._speaker_enrollment = {"enrollment": {
            "segments": {index: {"pcm": b"pcm", "quality": {}, "label": "测试"}
                         for index in range(3)},
        }}
        runtime._embed_speaker_audio = AsyncMock(side_effect=RuntimeError("嵌入提取失败"))
        with pytest.raises(RuntimeError, match="嵌入提取失败"):
            await runtime.commit_speaker_enrollment("enrollment")
        assert runtime._speaker_engine is None
        assert runtime._speaker_enrollment["enrollment"]["committing"] is False
    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_analysis", [False, True])
async def test_speaker_switch_cancel_preserves_client_until_session_cleanup(pending_analysis):
    """等待旧分析或嵌入请求时取消，运行时必须仍能关闭旧客户端及发送任务。"""
    runtime = runtime_stub()
    client = SpeakerWorkerClient("ws://127.0.0.1:1/speaker")
    socket = client.socket = SimpleNamespace(close=AsyncMock())
    sender = client.send_task = asyncio.create_task(client._send_loop())
    pending = asyncio.get_running_loop().create_future()
    if not pending_analysis:
        client.pending_embed["pending"] = pending
    runtime.controller = SimpleNamespace(speaker_client=client, _speaker_analysis_task=pending if pending_analysis else None)
    runtime.speaker = client
    runtime._speaker_worker_process = SimpleNamespace(pid=123, returncode=None)
    task = asyncio.create_task(runtime.switch_speaker_compute_device("cpu"))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.speaker is client
        assert not sender.done()
        # 对应会话关闭中的真实客户端收尾，必须能找到待关闭对象。
        await runtime.speaker.close()
        assert sender.done()
        assert client.send_task is None and client.socket is None
        socket.close.assert_awaited_once()
    finally:
        pending.cancel()
        await client.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_speaker_process_stop_retains_owned_process_for_retry():
    """退出等待被取消时保留进程句柄，下次收尾能继续确认其退出。"""
    runtime = runtime_stub()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()
        process.returncode = 0
        return 0

    process = SimpleNamespace(pid=123, returncode=None, terminate=Mock(), wait=wait)
    runtime._speaker_worker_process = process
    runtime._speaker_worker_created_at = 100
    task = asyncio.create_task(runtime._stop_managed_speaker_worker())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._speaker_worker_process is process
    release.set()
    await runtime._stop_managed_speaker_worker()
    assert runtime._speaker_worker_process is None
    assert runtime._speaker_worker_created_at is None


@pytest.mark.asyncio
async def test_speaker_switch_cancel_waits_until_detached_socket_is_closed():
    """客户端已解绑连接但尚未关完时取消，不能让连接脱离收尾。"""
    runtime = runtime_stub()
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close_socket():
        entered.set()
        await release.wait()
        closed.set()

    client = SpeakerWorkerClient("ws://127.0.0.1:1/speaker")
    client.socket = SimpleNamespace(close=close_socket)
    client.send_task = asyncio.create_task(client._send_loop())
    runtime.speaker = client
    runtime.controller = SimpleNamespace(speaker_client=client)
    runtime._speaker_worker_process = SimpleNamespace(pid=123, returncode=None)
    task = asyncio.create_task(runtime.switch_speaker_compute_device("cpu"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert client.socket is None
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert runtime.speaker is client
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert client.send_task is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
