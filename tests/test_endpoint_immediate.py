import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from voice_terminal.controller.options import InputOptions
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace


def test_legacy_candidate_configuration_still_loads_without_delaying_vad_end():
    options = InputOptions.from_mapping({"candidate_ms": 5000, "recheck_ms": 1000}, Path.cwd())
    assert options.candidate_ms == 5000
    assert options.hard_silence_ms == 1400
    assert options.tts_hold_fallback_ms == 2000
    assert InputOptions.from_mapping({"tts_hold_fallback_ms": 2500}, Path.cwd()).tts_hold_fallback_ms == 2500
    try:
        InputOptions.from_mapping({"tts_hold_fallback_ms": 0}, Path.cwd())
    except ValueError as exc:
        assert "tts_hold_fallback_ms" in str(exc)
    else:
        raise AssertionError("non-positive TTS hold fallback must be rejected")


def test_vad_end_starts_one_check_without_candidate_wait_and_hard_silence_recovers():
    async def scenario():
        class Analysis:
            def __init__(self):
                self.calls = []
                self.started = asyncio.Event()

            async def analyze(self, operation, pcm, *_args):
                assert operation == "endpoint"
                self.calls.append(pcm)
                if len(self.calls) == 1:
                    self.started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        # Simulate a native result that still arrives after cancellation.
                        return {"result": {"probability": 0.9}, "elapsed_ms": 1}
                return {"result": {"probability": 0.1}, "elapsed_ms": 1}

        activities = [
            SimpleNamespace(speech_started=False, speech_ended=True, speech_end_sample=0,
                            speech_start_sample=None, is_speech=False),
            SimpleNamespace(speech_started=True, speech_ended=False, speech_end_sample=None,
                            speech_start_sample=3200, is_speech=True),
            SimpleNamespace(speech_started=False, speech_ended=True, speech_end_sample=3520,
                            speech_start_sample=None, is_speech=False),
            SimpleNamespace(speech_started=False, speech_ended=False, speech_end_sample=None,
                            speech_start_sample=None, is_speech=False),
        ]
        controller = object.__new__(RealtimeController)
        controller.options = InputOptions(smart_turn=True, candidate_ms=5000, hard_silence_ms=1400)
        controller.analysis = Analysis()
        controller.workspace = TurnWorkspace("turn-a", started_at=1.0)
        controller.state = SimpleNamespace(listening=True, input_owner="asr")
        controller.vad = SimpleNamespace(feed=lambda _pcm: activities.pop(0))
        controller.audio_processor = None
        controller.wake_enabled = controller.wake_always_on = False
        controller.wake_preroll_ms = 0
        controller.speaker_client = None
        controller._asr_queue_overflow = False
        controller._last_frame = None
        controller._recent = deque()
        controller._vad_origin = 0
        controller._vad_generation = 0
        controller._reset_vad_pending = False
        controller._speech_start_sample = None
        controller._speech_end_sample = None
        controller._speech_samples = 0
        controller._active_input = True
        controller._endpoint_task = None
        controller._sub_id = "asr-a"
        hold_events = []
        controller._start_tts_hold_fallback = lambda: hold_events.append("silence")
        controller._cancel_tts_hold_fallback = lambda: hold_events.append("speech")
        controller._queue_asr = lambda _chunk: None
        controller._observe_activity = lambda activity, _frame: asyncio.sleep(0, result=activity)
        controller._segment = lambda *_args, **_kwargs: asyncio.sleep(0)
        controller._detail = lambda *_args, **_kwargs: asyncio.sleep(0)
        controller._track = lambda _task: None
        freezes = []

        async def freeze(reason, _frame):
            freezes.append(reason)
            controller._active_input = False

        controller._freeze = freeze
        await controller.handle_frame(AudioFrame(b"\0\0" * 3200, 0, 1.2))
        await asyncio.wait_for(controller.analysis.started.wait(), 1)
        assert len(controller.analysis.calls) == 1  # 200 ms pause, below old 500 ms gate.

        await controller.handle_frame(AudioFrame(b"\0\0" * 320, 3200, 1.22))
        await asyncio.sleep(0)
        assert freezes == []  # The late 0.9 result cannot close resumed speech.

        await controller.handle_frame(AudioFrame(b"\0\0" * 320, 3520, 1.24))
        await asyncio.sleep(0.01)
        assert len(controller.analysis.calls) == 2
        assert len(controller.analysis.calls[1]) == 3840 * 2  # Previous audio was retained.

        await controller.handle_frame(AudioFrame(b"\0\0" * 22400, 3840, 2.64))
        assert freezes == ["hard_silence"]
        assert hold_events == ["silence", "speech", "silence"]

    asyncio.run(scenario())


def test_smart_turn_positive_releases_held_tts_before_freezing_input():
    async def scenario():
        class Analysis:
            async def analyze(self, *_args):
                return {"result": {"probability": 0.9}, "elapsed_ms": 10}

        class Output:
            def __init__(self):
                self.playback_hold_active = True
                self.reasons = []

            def release_held(self, *, reason):
                self.reasons.append(reason)
                self.playback_hold_active = False

        controller = object.__new__(RealtimeController)
        controller.options = InputOptions(smart_turn=True, threshold=0.5)
        controller.analysis = Analysis()
        controller.workspace = TurnWorkspace("turn-smart")
        controller._active_input = True
        controller._endpoint_task = None
        controller._last_frame = AudioFrame(b"\0\0" * 320, 0, 1.0)
        controller.output = Output()
        controller._tts_hold_fallback_task = None
        controller._tts_hold_fallback_generation = 0
        controller._detail = lambda *_args, **_kwargs: asyncio.sleep(0)
        frozen = []

        async def freeze(reason, _frame):
            frozen.append(reason)
            controller._active_input = False

        controller._freeze = freeze
        task = asyncio.create_task(controller._endpoint(
            controller.workspace, controller._last_frame, b"\0\0" * 320,
        ))
        controller._endpoint_task = task
        await task

        assert controller.output.reasons == ["smart_turn"]
        assert frozen == ["smart_turn"]

    asyncio.run(scenario())


def test_smart_turn_continue_keeps_held_tts_for_the_fallback_deadline():
    async def scenario():
        class Analysis:
            async def analyze(self, *_args):
                return {"result": {"probability": 0.1}, "elapsed_ms": 10}

        class Output:
            playback_hold_active = True

            def release_held(self, *, reason):
                raise AssertionError(f"SmartTurn continue must not release TTS: {reason}")

        controller = object.__new__(RealtimeController)
        controller.options = InputOptions(smart_turn=True, threshold=0.5)
        controller.analysis = Analysis()
        controller.workspace = TurnWorkspace("turn-continue")
        controller._active_input = True
        controller._endpoint_task = None
        controller._last_frame = AudioFrame(b"\0\0" * 320, 0, 1.0)
        controller.output = Output()
        controller._tts_hold_fallback_task = None
        controller._tts_hold_fallback_generation = 0
        controller._detail = lambda *_args, **_kwargs: asyncio.sleep(0)
        frozen = []
        controller._freeze = lambda reason, _frame: asyncio.sleep(0, result=frozen.append(reason))
        task = asyncio.create_task(controller._endpoint(
            controller.workspace, controller._last_frame, b"\0\0" * 320,
        ))
        controller._endpoint_task = task
        await task

        assert controller.output.playback_hold_active is True
        assert frozen == []

    asyncio.run(scenario())
