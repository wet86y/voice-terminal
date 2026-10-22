"""运行操作：自动停听、顺序、异步任务寿命与状态快照。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.server.runtime_operations import runtime_operation, runtime_operations
from voice_terminal.server.tts_voice_runtime import TtsVoiceOperationManager
from voice_terminal.application.state import ApplicationState
from voice_terminal.controller.turn_controller import InputController
from voice_terminal.core.state import RuntimeState


class Runtime:
    def __init__(self, listening=True):
        self.state = SimpleNamespace(listening=listening, state=SimpleNamespace(value="IDLE"))
        self._session_lock = asyncio.Lock()
        self.stopped = asyncio.Event()
        self.controller = SimpleNamespace(stop_listening=self.stop, _active_input=False,
                                          episode=SimpleNamespace(active_request=None))
        self.bus = SimpleNamespace(publish=AsyncMock())
        self.start_listening = AsyncMock(side_effect=self.resume)
        self.tts_switch_busy = lambda: False
        self.stop_count = 0

    async def stop(self):
        self.stop_count += 1
        self.state.listening = False
        self.stopped.set()

    async def resume(self):
        self.state.listening = True

    def snapshot(self):
        return {"runtime_operations": runtime_operations(self).registry.snapshot()}


@pytest.mark.asyncio
@pytest.mark.parametrize("listening", [True, False])
async def test_operation_restores_only_original_listening_state(listening):
    runtime = Runtime(listening)
    async with runtime_operation(runtime, "future_model", "未来组件", pause=True):
        assert not runtime.state.listening
    assert runtime.state.listening is listening
    assert runtime.stop_count == int(listening)
    assert runtime.start_listening.await_count == int(listening)
    assert runtime_operations(runtime).registry.snapshot()[-1]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_consecutive_operations_do_not_reopen_microphone_between_tasks():
    runtime = Runtime()
    first_entered, first_release, second_entered, second_release = (asyncio.Event() for _ in range(4))
    async def work(name, entered, release):
        async with runtime_operation(runtime, name, name, pause=True):
            entered.set()
            await release.wait()
    first = asyncio.create_task(work("first", first_entered, first_release))
    await first_entered.wait()
    second = asyncio.create_task(work("second", second_entered, second_release))
    await asyncio.sleep(0)
    assert runtime_operations(runtime).holds == 2
    first_release.set()
    await second_entered.wait()
    runtime.start_listening.assert_not_awaited()
    second_release.set()
    await asyncio.gather(first, second)
    runtime.start_listening.assert_awaited_once()
    assert runtime.stop_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["queued", "stop", "close"])
async def test_restore_rechecks_permission_after_asr_health_before_opening_microphone(interruption):
    runtime = Runtime()
    runtime.state = RuntimeState()
    runtime.state.listening = True
    for service in ("audio", "asr", "llm", "tts"):
        runtime.state.set_service(service, "READY")
    runtime.state.set_service("kws", "DISABLED")
    checking, release_health, second_entered, release_second = (asyncio.Event() for _ in range(4))

    async def health():
        checking.set()
        await release_health.wait()

    class LifecycleController(InputController):
        async def _on_asr_result(self, *_):
            pass

    capture = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    controller = LifecycleController(
        state=runtime.state, bus=runtime.bus, history=SimpleNamespace(records=[]), capture=capture,
        kws=None, vad=SimpleNamespace(reset=lambda: None),
        asr=SimpleNamespace(connected=True, health=health), llm=None, tts=None, player=None,
        wake_enabled=False, wake_keyword="你好", wake_preroll_ms=0, vad_min_speech_ms=45,
        vad_end_silence_ms=500, max_turn_ms=30000, bridge_min_chars=1, bridge_max_chars=36,
        bridge_max_wait_ms=100, bridge_breaks="。", listening_start_allowed=runtime_operations(runtime).listening_start_allowed,
    )
    controller.stop_listening = runtime.stop
    controller._listen_loop = lambda: asyncio.Event().wait()
    runtime.controller = controller
    runtime.start_listening = controller.start_listening

    async def first():
        async with runtime_operation(runtime, "first", "设备切换", pause=True):
            pass

    async def second():
        async with runtime_operation(runtime, "second", "后续维护", pause=True):
            second_entered.set()
            await release_second.wait()

    first_task = asyncio.create_task(first())
    second_task = None
    try:
        await asyncio.wait_for(checking.wait(), 5)
        if interruption == "queued":
            second_task = asyncio.create_task(second())
            await asyncio.sleep(0)
            assert runtime_operations(runtime).holds == 1
        elif interruption == "stop":
            runtime_operations(runtime).intent(False)
        else:
            runtime._application_stopping = True
        release_health.set()
        await asyncio.wait_for(first_task, 5)
        if second_task is not None:
            await asyncio.wait_for(second_entered.wait(), 5)
        capture.start.assert_not_awaited()
        assert not runtime.state.listening
    finally:
        release_health.set()
        release_second.set()
        await asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)
        if controller._listen_task is not None:
            controller._listen_task.cancel()
            await asyncio.gather(controller._listen_task, return_exceptions=True)
    if interruption == "queued":
        capture.start.assert_awaited_once()
        assert runtime.state.listening


@pytest.mark.asyncio
async def test_waits_for_language_model_request_even_before_audio_exists():
    runtime = Runtime()
    runtime.controller.episode.active_request = object()
    entered = asyncio.Event()
    async def work():
        async with runtime_operation(runtime, "preview", "试听", pause=True, wait_output=True):
            entered.set()
    task = asyncio.create_task(work())
    await runtime.stopped.wait()
    await asyncio.sleep(.01)
    assert not entered.is_set()
    runtime.controller.episode.active_request = None
    await asyncio.wait_for(task, 1)
    assert entered.is_set() and runtime.state.listening


@pytest.mark.asyncio
async def test_idle_capture_does_not_wait_for_unrelated_startup_state():
    runtime = Runtime(False)
    runtime.state.state.value = "STARTING"
    async with runtime_operation(runtime, "capture", "声纹录制", pause=True, wait_output=True):
        assert not runtime.state.listening
    runtime.start_listening.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_queued_operation_releases_only_its_lease():
    runtime = Runtime()
    entered, release = asyncio.Event(), asyncio.Event()
    async def first():
        async with runtime_operation(runtime, "first", "切换", pause=True):
            entered.set()
            await release.wait()
    async def second():
        async with runtime_operation(runtime, "second", "录音", pause=True):
            pytest.fail("排队取消的任务不能执行")
    first_task = asyncio.create_task(first())
    await entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    second_task.cancel()
    await asyncio.gather(second_task, return_exceptions=True)
    assert runtime_operations(runtime).holds == 1 and not runtime.state.listening
    release.set()
    await first_task
    assert runtime_operations(runtime).registry.snapshot()[-1]["state"] == "cancelled"
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_task_reserves_order_before_accepted_response():
    runtime = Runtime()
    manager = TtsVoiceOperationManager(runtime)
    order = []
    async def play(_):
        order.append("play")
        return {}
    accepted = manager._new("play", {"model": "future"}, play, require_library=False)
    # 同一协程在后台任务第一次调度前，立即发起另一个切换。
    async with runtime_operation(runtime, "selection", "语音合成", pause=True):
        order.append("selection")
    await manager._items[accepted["operation_id"]].task
    assert order == ["play", "selection"]
    runtime.start_listening.assert_awaited_once()
    assert not manager.busy


@pytest.mark.asyncio
async def test_cancelling_voice_task_before_first_schedule_does_not_leak():
    runtime = Runtime()
    manager = TtsVoiceOperationManager(runtime)
    runner = AsyncMock()
    accepted = manager._new("play", {"model": "future"}, runner, require_library=False)
    item = manager._items[accepted["operation_id"]]
    manager.cancel(item.id)
    await asyncio.gather(item.task, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not manager.busy and runtime_operations(runtime).holds == 0
    assert item.state == "cancelled" and item.completed_monotonic is not None
    runner.assert_not_awaited()
    assert runtime.state.listening


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["close", "rollback", "explicit_stop"])
async def test_unsafe_or_invalidated_operation_does_not_resume(reason):
    runtime = Runtime()
    async with runtime_operation(runtime, "switch", "切换", pause=True) as operation:
        if reason == "close":
            runtime._closed = True
        elif reason == "rollback":
            operation.prevent_resume()
            operation.fail("原模型恢复失败")
        else:
            runtime_operations(runtime).intent(False)
    runtime.start_listening.assert_not_awaited()
    assert not runtime.state.listening


@pytest.mark.asyncio
async def test_failed_resume_has_terminal_status_and_propagates_error():
    runtime = Runtime()
    runtime.start_listening = AsyncMock()
    with pytest.raises(RuntimeError, match="监听未能恢复"):
        async with runtime_operation(runtime, "switch", "切换", pause=True):
            pass
    assert runtime_operations(runtime).holds == 0
    assert runtime_operations(runtime).registry.snapshot()[-1]["state"] == "failed"


def test_host_recovery_uses_same_contract_and_retains_terminal_snapshot():
    state = ApplicationState()
    state.component("asr", retrying=True, phase="等待恢复")
    running = state.snapshot()["runtime_operations"][-1]
    assert running["state"] == "running" and running["phase"] == "restarting"
    state.component("asr", state="ready", error="", retrying=False)
    complete = state.snapshot()["runtime_operations"][-1]
    assert complete["id"] == running["id"] and complete["revision"] > running["revision"]
    assert complete["state"] == "succeeded"
    state.component("asr", retrying=True)
    state.component("asr", state="ready", error="显卡回退，处理器已就绪", retrying=False)
    assert state.snapshot()["runtime_operations"][-1]["state"] == "succeeded"
    state.component("asr", retrying=True)
    state.component("asr", retrying=False, operation_failed=True)
    assert state.snapshot()["runtime_operations"][-1]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("close", [False, True])
async def test_cancel_or_close_during_resume_still_finishes_cleanup(close):
    runtime = Runtime()
    restoring, release = asyncio.Event(), asyncio.Event()
    async def publish(_):
        if runtime_operations(runtime).restoring:
            restoring.set()
            await release.wait()
    runtime.bus.publish = publish
    async def work():
        async with runtime_operation(runtime, "switch", "切换", pause=True):
            pass
    task = asyncio.create_task(work())
    await restoring.wait()
    if close:
        runtime._closed = True
        runtime_operations(runtime).intent(False)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert runtime_operations(runtime).holds == 0
    assert runtime_operations(runtime).registry.snapshot()[-1]["state"] == "cancelled"
    assert runtime.state.listening is not close


@pytest.mark.asyncio
async def test_native_capture_releases_microphone_before_resuming(tmp_path, monkeypatch):
    import threading
    from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
    class SpeakerRuntime(Runtime, SpeakerRuntimeMixin):
        pass
    runtime = SpeakerRuntime()
    runtime._speaker_enrollment = {}
    runtime.audio_processor = None
    runtime.config = SimpleNamespace(audio=SimpleNamespace(sample_rate=16000, channels=1, input_device=None))
    started, exited, release = threading.Event(), threading.Event(), threading.Event()
    def capture(**kwargs):
        started.set()
        kwargs["control"].cancel.wait(2)
        release.wait(2)
        exited.set()
        return SimpleNamespace(duration_ms=5000)
    monkeypatch.setattr("voice_terminal.server.speaker_runtime.capture_segment", capture)
    async def record():
        async with runtime._capture_speaker_pcm(owner="test", seconds=5, capture_id="capture"):
            pass
    task = asyncio.create_task(record())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(.01)
    assert not exited.is_set() and not runtime.state.listening
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert exited.is_set() and runtime.state.listening
    assert not runtime._speaker_captures
