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


def runtime_stub():
    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime.config = SimpleNamespace(
        speaker_model_path=Path("test-model.onnx"),
        speaker=SimpleNamespace(model_id="wespeaker-cnceleb-resnet34"),
        audio=SimpleNamespace(sample_rate=16000),
        compute=SimpleNamespace(devices={"wespeaker": "cpu"}, fallback_devices={}),
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
