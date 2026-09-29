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

    asyncio.run(scenario())
