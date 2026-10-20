"""唤醒并行准备、连续音频回放和即时模型调度的回归测试。"""
import asyncio
from array import array
from collections import deque
from types import SimpleNamespace
import pytest

from voice_terminal.audio.player import AudioPlayerError, PcmPlayer
from voice_terminal.audio.wake_cue import make_wake_cue
from voice_terminal.controller.input_ownership import InputOwnershipMixin
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.core.state import RuntimeState
from voice_terminal.kws.service import KwsDetection
from tests.test_scheduler_staircase import _BlockingProvider, _speaking_runtime


class WakeHarness(InputOwnershipMixin):
    """使用真实唤醒准备逻辑，替换外部服务与音频设备。"""
    def __init__(self):
        self.state = RuntimeState(listening=True, input_owner="kws")
        self.wake_always_on = True
        self.wake_enabled = True
        self._activation_generation = 0
        self._turn_timings = {}
        self.workspace = None
        self._background = set()
        self.events = []
        self.replayed = []
        self.health_release = asyncio.Event()
        self.cue_release = asyncio.Event()
        self.quiet_policy = SimpleNamespace(user_interaction=False)
        self.kws = SimpleNamespace(error=None, running=True, reset=lambda: None)
        self.asr = SimpleNamespace(connected=True, health=self.health)
        self.player = SimpleNamespace(play_wake_cue=self.cue,
                                      wait_until_output_complete=self.cue_wait,
                                      clear=lambda key: self.events.append(("clear", key)))

    async def health(self):
        self.events.append("health")
        await self.health_release.wait()

    def cue(self, key):
        assert self.quiet_policy.user_interaction
        self.events.append("cue")

    async def cue_wait(self, **kwargs):
        await self.cue_release.wait()

    def _track(self, task):
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _start_tts_prewarm(self, ws):
        self.events.append(("prewarm", ws.turn_id))

    async def _cancel_tts_prewarm(self, turn_id):
        self.events.append(("discard", turn_id))

    async def _reset_owner_boundary(self):
        self.events.append("reset")

    async def _on_kws_activated(self, source_id, generation):
        self.events.append(("llm", source_id))
        return True

    async def _state_event(self, *args, **kwargs):
        pass

    async def _error(self, code, message, **kwargs):
        self.events.append(("error", code))

    async def handle_frame(self, frame, *, _wake_replay=False):
        if _wake_replay:
            self.replayed.append(frame)
        else:
            await self._buffer_wake_frame(frame)


def test_wake_branches_start_before_health_and_output_waits_for_replay_and_cue():
    async def scenario():
        controller = WakeHarness()
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        await controller._activate_kws_detection()
        await asyncio.sleep(0)
        assert "cue" in controller.events and "health" in controller.events
        assert [e for e in controller.events if isinstance(e, tuple) and e[0] == "llm"] == [("llm", context.source_id)]
        assert ("prewarm", context.source_id) in controller.events
        assert controller.state.input_owner == "kws"
        output = asyncio.create_task(controller._wait_for_wake_output(context.source_id))
        for index in range(3):
            assert await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, index * 320, index * .02))
        controller.health_release.set()
        await asyncio.sleep(0)
        assert context.ready and not context.drained.is_set()
        assert not output.done()
        assert await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, 960, .06))
        assert [f.start_sample for f in controller.replayed] == [0, 320, 640, 960]
        assert context.drained.is_set() and not context.cue_done.is_set()
        controller.cue_release.set()
        await asyncio.wait_for(output, 1)
        assert not await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, 1280, .08))
        await controller._cancel_wake_preparation()
    asyncio.run(scenario())


def test_failed_health_and_cancel_never_restore_stale_input():
    async def scenario():
        controller = WakeHarness()
        async def failed_health():
            raise RuntimeError("健康检查失败")
        controller.asr.health = failed_health
        await controller._activate_kws_detection()
        await controller._wake_preparation.tasks[1]
        assert controller._wake_preparation is None
        assert controller.state.input_owner == "kws"
        assert ("error", "ASR_WORKER_NOT_READY") in controller.events
        controller = WakeHarness()
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        await asyncio.sleep(0)
        await controller._cancel_wake_preparation()
        controller.state.listening = False
        controller.health_release.set()
        await asyncio.sleep(0)
        assert context.failed and context.drained.is_set()
        assert controller.state.input_owner == "kws"
    asyncio.run(scenario())


def test_wake_buffer_rejects_gap_and_overflow():
    async def scenario():
        for gap in (True, False):
            controller = WakeHarness()
            await controller._activate_kws_detection()
            await asyncio.sleep(0)
            await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, 0, 0))
            frame = AudioFrame(b"\0\0" * (320 if gap else 16000 * 8), 640 if gap else 320, .02)
            await controller._buffer_wake_frame(frame)
            assert controller._wake_preparation is None
            assert not controller.replayed
            assert controller.state.input_owner == "kws"
    asyncio.run(scenario())


def test_first_post_detection_frame_must_start_at_detection_end():
    async def scenario():
        for seeded in (False, True):
            controller = WakeHarness()
            if seeded:
                controller._kws_recent = deque([
                    AudioFrame(b"\0\0" * 320, 0, 0),
                    AudioFrame(b"\0\0" * 320, 640, .04),
                ])
            await controller._activate_kws_detection(KwsDetection("七三", 0, 320, 0))
            if not seeded:
                await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, 640, .04))
            assert controller._wake_preparation is None
            assert ("error", "ASR_WORKER_NOT_READY") in controller.events
            assert not controller.replayed
    asyncio.run(scenario())


def test_cue_failure_does_not_stop_asr_or_model_preparation():
    async def scenario():
        controller = WakeHarness()
        def fail_cue(key):
            raise RuntimeError("扬声器暂时不可用")
        controller.player.play_wake_cue = fail_cue
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        await context.tasks[0]
        assert context.cue_done.is_set() and not context.failed
        assert ("llm", context.source_id) in controller.events
        assert ("prewarm", context.source_id) in controller.events
        controller.health_release.set()
        await context.tasks[1]
        assert controller.state.input_owner == "asr"
        await controller._cancel_wake_preparation()
    asyncio.run(scenario())


def test_local_cue_is_bounded_and_uses_existing_render_reference():
    pcm = make_wake_cue(32000, 2)
    assert len(pcm) == 32000 * 180 // 1000 * 2 * 2
    samples = array("h", pcm)
    assert max(abs(value) for value in samples) <= round(32767 * .15)
    assert samples[::2] == samples[1::2]
    player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
    player.start = lambda: None
    rendered, started = [], []
    player.on_audio_rendered = rendered.append
    player.on_playback_started = started.append
    player.play_wake_cue("cue-wake")
    player._callback(bytearray(360), 180, None, None)
    assert started == ["cue-wake"] and rendered == [player._wake_cue_pcm]


def test_cue_cannot_replace_audio_in_the_device_output_tail():
    player = PcmPlayer(sample_rate=1000, channels=1, output_device=None, device_latency_ms=120)
    player.start = lambda: None
    player.enqueue("formal-speech", b"\1\0" * 180)
    player._callback(bytearray(360), 180, None, None)
    assert player.buffered_ms == 0
    with pytest.raises(AudioPlayerError):
        player.play_wake_cue("cue-wake")
    assert player.progress()["key"] == "formal-speech"


def test_delayed_detection_preserves_all_post_wake_frames_and_drains_live_capture():
    async def scenario():
        controller = WakeHarness()
        controller._kws_recent = deque(AudioFrame(b"\0\0" * 320, index * 320, index * .02)
                                        for index in range(5))
        detection = KwsDetection("七三", 0, 320, 0)
        await controller._activate_kws_detection(detection)
        context = controller._wake_preparation
        assert context.detection is detection
        assert [f.start_sample for f in context.frames] == [320, 640, 960, 1280]
        controller.health_release.set()
        await context.tasks[1]
        controller.capture = SimpleNamespace(queue=asyncio.Queue())
        controller.capture.queue.put_nowait(AudioFrame(b"\0\0" * 320, 1920, .12))
        await controller._buffer_wake_frame(AudioFrame(b"\0\0" * 320, 1600, .1))
        assert [f.start_sample for f in controller.replayed] == [320, 640, 960, 1280, 1600, 1920]
        assert controller.capture.queue.empty()
        await controller._cancel_wake_preparation()
    asyncio.run(scenario())


def test_obsolete_wake_candidate_yields_to_user_input():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime._ensure_runner = lambda: None
        await runtime.schedule_system_check("kws_wake", "wake-old", arm_silence=True,
                                            immediate_dispatch=True)
        runtime.pending.add("USER_SEGMENT_UPDATED")
        assert not await runtime._dispatch_immediate_system_check()
        assert runtime._immediate_system_check is None
        assert "USER_SEGMENT_UPDATED" in runtime.pending
        assert provider.calls == 0
        await runtime.stop()
    asyncio.run(scenario())


def test_real_prewarm_lifecycle_is_bound_to_wake_and_cancellation_is_scoped():
    async def scenario():
        class WarmHarness(WakeHarness):
            _start_tts_prewarm = RealtimeController._start_tts_prewarm
            _run_tts_prewarm = RealtimeController._run_tts_prewarm
            _cancel_tts_prewarm = RealtimeController._cancel_tts_prewarm

            async def _publish_tts_prewarm(self, ws, status, **kwargs):
                self.events.append(("warm_status", status))

        for status in ("READY", "FALLBACK", "FAILED", "DISABLED"):
            controller = WarmHarness()
            controller._tts_prewarm_tasks = {}
            handshake = asyncio.Event()
            started = asyncio.Event()
            discarded = []

            async def prewarm(*, turn_id):
                assert turn_id == controller._wake_preparation.source_id
                started.set()
                await handshake.wait()
                if status == "FAILED":
                    raise RuntimeError("预热握手失败")
                return {"status": status}

            async def discard(*, turn_id=None):
                discarded.append(turn_id)

            controller.tts = SimpleNamespace(transport="local" if status == "DISABLED" else "websocket",
                                              prewarm=prewarm, discard_prewarm=discard)
            await controller._activate_kws_detection()
            context = controller._wake_preparation
            if status != "DISABLED":
                await asyncio.wait_for(started.wait(), 1)
                assert not context.ready
                task = controller._tts_prewarm_tasks[context.source_id]
                handshake.set()
                await task
            assert context.workspace.tts_prewarm_status == status
            await controller._cancel_wake_preparation()
            assert discarded == [context.source_id]
            assert not controller._tts_prewarm_tasks

        # 清理唤醒时不得关闭已经移交用户回合的预热连接。
        controller = WakeHarness()
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        context.prewarm_released = True
        await controller._cancel_wake_preparation()
        assert ("discard", context.source_id) not in controller.events
    asyncio.run(scenario())


def test_capture_failure_cleans_preparation_and_wakes_output_waiters():
    async def scenario():
        controller = WakeHarness()
        async def fail_read():
            raise RuntimeError("采集设备断开")
        async def nothing(*args, **kwargs):
            pass
        controller.capture = SimpleNamespace(read_frame=fail_read, stop=nothing)
        controller.asr.close = nothing
        controller._cancel_turn = nothing
        controller._asr_sender = None
        controller._asr_queue = asyncio.Queue()
        controller._closed = False
        controller.vad = SimpleNamespace()
        controller.wake_enabled = True
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        await asyncio.sleep(0)
        await controller._listen_loop()
        assert not controller.state.listening
        assert controller._wake_preparation is None
        assert context.drained.is_set() and context.cue_done.is_set()
        assert all(task.done() for task in context.tasks)
        assert ("discard", context.source_id) in controller.events
    asyncio.run(scenario())


def test_original_frame_path_processes_buffered_audio_once_before_vad_replay():
    async def scenario():
        class FrameHarness(WakeHarness):
            handle_frame = RealtimeController.handle_frame

            async def _observe_activity(self, activity, frame):
                self.replayed.append(frame)
                return None

        controller = FrameHarness()
        processed = []
        def process(pcm):
            processed.append(pcm)
            return b"\1\0" * (len(pcm) // 2)
        controller._process_audio = process
        controller.audio_processor = SimpleNamespace(enabled=True)
        controller._last_frame = None
        controller._asr_queue_overflow = False
        controller._recent = deque()
        controller._vad_origin = None
        controller._reset_vad_pending = False
        controller._vad_generation = 0
        controller._active_input = False
        controller.workspace = None
        controller.wake_preroll_ms = 0
        controller.vad = SimpleNamespace(feed=lambda pcm: SimpleNamespace(
            speech_started=False, speech_ended=False, speech_end_sample=None, is_speech=False))
        await controller._activate_kws_detection()
        context = controller._wake_preparation
        for index in range(2):
            await controller.handle_frame(AudioFrame(b"\0\0" * 320, index * 320, index * .02))
        assert not controller.replayed
        controller.health_release.set()
        await context.tasks[1]
        await controller.handle_frame(AudioFrame(b"\0\0" * 320, 640, .04))
        assert len(processed) == 3
        assert [f.start_sample for f in controller.replayed] == [0, 320, 640]
        assert all(f.pcm == b"\1\0" * 320 for f in controller.replayed)
        await controller._cancel_wake_preparation()
    asyncio.run(scenario())


def test_pending_wake_dispatches_when_previous_request_finishes_without_heartbeat():
    async def scenario():
        class TwoCallProvider:
            transport = "responses"
            calls = 0
            def __init__(self):
                self.releases = [asyncio.Event(), asyncio.Event()]
            def stream_events(self, messages, *, json_mode=False):
                release = self.releases[self.calls]
                self.calls += 1
                async def stream():
                    await release.wait()
                    yield {"type": "content", "text": '{"speak":{"state":"wait"},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        provider = TwoCallProvider()
        runtime = _speaking_runtime(provider)
        assert await runtime._start_dispatch({"USER_SEGMENT_UPDATED"})
        await asyncio.sleep(0)
        assert provider.calls == 1
        await runtime.schedule_system_check("kws_wake", "wake-after-busy", arm_silence=True,
                                            immediate_dispatch=True)
        await asyncio.sleep(0)
        assert provider.calls == 1
        provider.releases[0].set()
        for _ in range(50):
            await asyncio.sleep(0)
            if provider.calls == 2:
                break
        assert provider.calls == 2
        assert runtime.active_request_source_turn_id == "wake-after-busy"
        await runtime.stop()
    asyncio.run(scenario())


def test_kws_return_rechecks_new_input_after_async_cleanup():
    async def scenario():
        controller = WakeHarness()
        controller.state.input_owner = "asr"
        cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()
        async def cleanup():
            cleanup_started.set()
            await cleanup_release.wait()
        controller._cancel_wake_preparation = cleanup
        returning = asyncio.create_task(controller._return_to_kws(0))
        await cleanup_started.wait()
        controller.workspace = SimpleNamespace(turn_id="new-user-input")
        cleanup_release.set()
        assert not await returning
        assert controller.state.input_owner == "asr"
    asyncio.run(scenario())


def test_session_reset_blocks_audio_during_async_cleanup_and_releases_on_failure():
    async def scenario():
        for failed in (False, True):
            entered, release = asyncio.Event(), asyncio.Event()
            async def reset_state():
                entered.set()
                await release.wait()
                if failed:
                    raise RuntimeError("模拟重置失败")
            controller = SimpleNamespace(_reset_session_state=reset_state)
            reset = asyncio.create_task(EpisodeController.reset_session(controller))
            await entered.wait()
            # 未配置识别/检测器；重置期间真实入口应直接丢弃过渡帧。
            await RealtimeController.handle_frame(controller, AudioFrame(b"\0\0" * 320, 0, 0))
            assert controller._input_resetting
            release.set()
            if failed:
                with pytest.raises(RuntimeError):
                    await reset
            else:
                await reset
            assert not controller._input_resetting
    asyncio.run(scenario())


def test_immediate_wake_runs_without_a_heartbeat_and_coalesces_queue_signal():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.inputs.clear()
        runtime.last_turn_id = None
        # 无心跳线程；真实串行循环由系统检查的队列信号唤醒。
        runtime.queue.put_nowait(True)
        await runtime.schedule_system_check("kws_wake", "wake-now", arm_silence=True,
                                            immediate_dispatch=True)
        for _ in range(30):
            await asyncio.sleep(0)
            if provider.calls:
                break
        assert provider.calls == 1
        assert runtime.active_request_source_turn_id == "wake-now"
        assert runtime.inputs == {}
        assert not await runtime._dispatch_immediate_system_check()
        await runtime.stop()
    asyncio.run(scenario())
