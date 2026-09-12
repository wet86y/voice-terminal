from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
import unittest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.turn_workspace import AudioFrame, CurrentTurnAudioBuffer
from voice_terminal.core.state import RuntimeState, TerminalState
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


class InputContinuityRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _controller(self) -> RealtimeController:
        controller = object.__new__(RealtimeController)
        controller.workspace = SimpleNamespace(turn_id="turn-gap")
        controller.capture = SimpleNamespace(diagnostics={"dropped_chunks": 2})
        controller._asr_queue = asyncio.Queue(maxsize=2)
        controller._asr_queue_overflow = True
        controller._asr_queue_overflow_at = 1.0
        controller._recent = __import__("collections").deque()
        controller._last_frame = AudioFrame(b"\x00\x00" * 10, 0, 1.0)
        controller._vad_origin = 0
        controller._speech_start_sample = 0
        controller._speech_end_sample = 10
        controller._speech_samples = 10
        controller._resume_started = 0
        controller._vad_generation = 3
        controller._reset_vad_pending = True
        controller.barge_in_gate = SimpleNamespace(reset=lambda: None)
        controller.vad = SimpleNamespace(reset=lambda: None)
        controller.state = RuntimeState()
        controller.cancelled: list[tuple[str, bool]] = []
        controller.errors: list[tuple[str, bool]] = []
        controller.states: list[tuple[TerminalState, bool | None]] = []

        async def cancel_turn(*, reason, record):
            controller.cancelled.append((reason, record))
            controller.workspace = None

        async def error(code, _message, *, recoverable, turn_id=None):
            controller.errors.append((code, recoverable))

        async def state_event(state, *, listening=None, turn_id=None):
            controller.states.append((state, listening))

        controller._cancel_turn = cancel_turn
        controller._error = error
        controller._state_event = state_event
        return controller

    async def test_capture_gap_discards_only_current_input_and_keeps_listening(self) -> None:
        controller = self._controller()
        controller._asr_queue.put_nowait("old-audio")
        frame = AudioFrame(b"\x00\x00" * 10, 40, 2.0)

        await controller._recover_input_continuity(
            frame,
            reason="capture_gap",
            expected_start_sample=10,
        )

        self.assertEqual(controller.cancelled, [("capture_gap", True)])
        self.assertTrue(controller._asr_queue.empty())
        self.assertFalse(controller._asr_queue_overflow)
        self.assertEqual(controller._last_frame, frame)
        self.assertEqual(controller._vad_origin, 40)
        self.assertEqual(controller.errors, [("AUDIO_CAPTURE_GAP", True)])
        self.assertEqual(controller.states, [(TerminalState.LISTENING, True)])
        self.assertEqual(controller.state.current["capture_diagnostics"]["dropped_chunks"], 2)

    async def test_frame_processing_error_is_recoverable_not_a_session_stop(self) -> None:
        controller = self._controller()

        class OneFrameCapture:
            async def read_frame(self) -> AudioFrame:
                return AudioFrame(b"\x00\x00" * 10, 42, 2.0)

        controller.capture = OneFrameCapture()
        controller._closed = False
        controller.state.listening = True

        async def fail_frame(_frame: AudioFrame) -> None:
            raise RuntimeError("simulated VAD callback failure")

        original_recover = controller._recover_input_continuity

        async def recover_once(*args, **kwargs) -> None:
            await original_recover(*args, **kwargs)
            controller._closed = True

        controller.handle_frame = fail_frame
        controller._recover_input_continuity = recover_once

        await controller._listen_loop()

        self.assertTrue(controller.state.listening)
        self.assertIn(("AUDIO_INPUT_FRAME_FAILED", True), controller.errors)
        detail = controller.state.current["capture_diagnostics"]
        self.assertEqual(detail["reason"], "frame_processing_error")
        self.assertEqual(detail["error_type"], "RuntimeError")

    async def test_protected_activity_skip_keeps_current_turn_pcm_contiguous(self) -> None:
        """A rejected speaker/AEC frame must not create a later buffer gap."""

        controller = object.__new__(RealtimeController)
        controller._asr_queue_overflow = False
        controller._last_frame = None
        controller.audio_processor = None
        controller._recent = __import__("collections").deque()
        controller.wake_preroll_ms = 0
        controller._vad_origin = None
        controller._reset_vad_pending = False
        controller._vad_generation = 0
        controller._speech_start_sample = None
        controller._speech_end_sample = None
        controller._speech_samples = 0
        controller._active_input = False
        controller.wake_enabled = False
        controller.vad = SimpleNamespace(
            feed=lambda _pcm: SimpleNamespace(speech_started=False, speech_end_sample=None),
        )
        controller.workspace = SimpleNamespace(audio=CurrentTurnAudioBuffer())

        async def protected_skip(_activity, _frame):
            return None

        controller._observe_activity = protected_skip

        await controller.handle_frame(AudioFrame(b"\x00\x00" * 320, 0, 1.0))
        await controller.handle_frame(AudioFrame(b"\x00\x00" * 320, 320, 1.02))

        self.assertEqual(controller.workspace.audio.start_sample, 0)
        self.assertEqual(controller.workspace.audio.end_sample, 640)

    def test_asr_queue_overflow_is_recorded_for_controlled_recovery(self) -> None:
        controller = self._controller()
        controller._asr_queue = asyncio.Queue(maxsize=1)
        controller._asr_queue.put_nowait("first")
        controller._asr_queue_overflow = False

        controller._queue_asr("second")

        self.assertTrue(controller._asr_queue_overflow)
        self.assertEqual(controller._asr_queue.qsize(), 1)


class PlaybackDiagnosticsTests(unittest.TestCase):
    def test_first_pcm_submission_is_distinct_from_provider_queueing(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        player.enqueue("speech-1", b"\x01\x00" * 10)

        queued = player.diagnostics
        self.assertIsNone(queued["first_rendered_at"])
        self.assertEqual(queued["active_key"], "speech-1")

        player._callback(bytearray(20), 10, None, None)
        submitted = player.diagnostics
        self.assertEqual(submitted["first_rendered_key"], "speech-1")
        self.assertIsInstance(submitted["first_rendered_at"], float)


class BargeInCompletionGuardTests(unittest.IsolatedAsyncioTestCase):
    def _controller(self, *, progress):
        controller = object.__new__(EpisodeController)
        controller.state = SimpleNamespace(listening=True)
        controller._barge_trace_state = (True, False, "speech")
        controller.player = SimpleNamespace(progress=lambda _key: dict(progress))
        workspace = SimpleNamespace(playback_started=True, playback_key="speech-1", generation=1)
        handle = SimpleNamespace(
            identity=("conversation-1", "request-1"), turn_id="turn-1", speech_id="speech-1",
            workspace=workspace, overlap_start_ms=None,
        )
        controller.output = SimpleNamespace(active=True, handle=handle)
        return controller

    async def test_drained_append_window_returns_speech_to_normal_input_path(self) -> None:
        controller = self._controller(progress={
            "enqueued_ms": 500, "played_ms": 500, "output_estimated_ms": 500,
        })
        calls = []
        resets = []

        async def speech_activity(value, **kwargs):
            calls.append((value, kwargs))

        def unexpected_feed(*_args, **_kwargs):
            self.fail("drained output must not enter the barge-in gate")

        controller.episode = SimpleNamespace(closed=False, speech_activity=speech_activity)
        controller.barge_in_gate = SimpleNamespace(feed=unexpected_feed, reset=lambda: resets.append(True))
        activity = SimpleNamespace(is_speech=True)

        returned = await controller._observe_activity(activity, SimpleNamespace(captured_at=1.0))

        self.assertIs(returned, activity)
        self.assertEqual(calls, [(True, {})])
        self.assertEqual(resets, [True])
        self.assertIsNone(controller._barge_trace_state)

    async def test_remaining_audio_keeps_real_barge_in_cancellation(self) -> None:
        controller = self._controller(progress={
            "enqueued_ms": 500, "played_ms": 250, "output_estimated_ms": 250,
        })
        calls = []
        resets = []
        traces = []

        async def speech_activity(value, **kwargs):
            calls.append((value, kwargs))

        async def log(kind, **payload):
            traces.append((kind, payload))

        controller.episode = SimpleNamespace(closed=False, speech_activity=speech_activity, log=log)
        controller.barge_in_gate = SimpleNamespace(
            feed=lambda *_args, **_kwargs: SimpleNamespace(
                candidate=True, confirmed=True, duration_ms=300, reason="near_voice",
            ),
            reset=lambda: resets.append(True),
        )
        controller._near_voice_activity = lambda: True
        controller._barge_in_protected_available = lambda: True
        activity = SimpleNamespace(is_speech=True)

        await controller._observe_activity(activity, SimpleNamespace(captured_at=1.0))

        self.assertEqual(calls, [(True, {"interrupt_reason": "user_interrupted"})])
        self.assertEqual(resets, [True])
        self.assertEqual([kind for kind, _payload in traces], ["BARGE_IN_CONFIRMED"])
        self.assertEqual(controller.output.handle.overlap_start_ms, 0)

    async def test_aec_unavailable_stop_policy_stops_output_without_raw_vad(self) -> None:
        controller = self._controller(progress={
            "enqueued_ms": 500, "played_ms": 250, "output_estimated_ms": 250,
        })
        cancelled, traces, resets = [], [], []

        async def log(kind, **payload):
            traces.append((kind, payload))

        controller.aec_failure_policy = "stop_and_listen"
        controller.audio_processor = SimpleNamespace(status="DEGRADED", error="reference stalled")
        controller._aec_recovery_task = None
        controller.output.cancel_all = lambda reason: cancelled.append(reason)
        controller.episode = SimpleNamespace(closed=False, log=log)
        controller.barge_in_gate = SimpleNamespace(reset=lambda: resets.append(True))

        await controller._handle_aec_unavailable(controller.output.handle)

        self.assertEqual(cancelled, ["input_unavailable"])
        self.assertEqual(resets, [True])
        self.assertEqual([kind for kind, _payload in traces], ["AEC_FAILED"])


class TurnSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_agent_snapshot_does_not_cancel_unsettled_tts_prewarm(self) -> None:
        """A stale idle snapshot must not tear down a prewarm needed by dispatch."""

        controller = object.__new__(EpisodeController)
        controller.state = RuntimeState()
        controller.workspace = None
        controller.output = SimpleNamespace(active=False)
        controller.episode = SimpleNamespace(last_turn_id="turn_a")
        cancelled = []
        published = []

        async def publish(item):
            published.append(item)

        async def cancel_prewarm(turn_id=None):
            cancelled.append(turn_id)

        controller._publish = publish
        controller._cancel_tts_prewarm = cancel_prewarm

        await controller._agent_event("agent.episode", {
            "turn_id": "turn_a",
            "agent_busy": False,
        })

        self.assertEqual(cancelled, [])
        self.assertEqual(controller.state.agent["turn_id"], "turn_a")
        self.assertEqual([item.type for item in published], ["agent.episode"])

    async def test_only_settlement_emits_one_terminal_turn_event(self) -> None:
        controller = object.__new__(EpisodeController)
        controller._settled_turns = set()
        controller._committed_workspaces = {
            "turn_a": SimpleNamespace(turn_id="turn_a", final_text="已定稿", episode_id="conversation_a",
                                      started_at=time.monotonic(), timings={}, tts_prewarm_status="DISABLED"),
        }
        controller._turn_timings = {}
        controller.history = SessionLedger()
        record = controller.history.append_record({"turn_id": "turn_a", "transcript": "已定稿"})
        controller.workspace = None
        controller.output = SimpleNamespace(active=False)
        controller.conversation = ConversationRuntime()
        controller.conversation.begin_turn("turn_a")
        controller.state = RuntimeState()
        controller.state.listening = True
        events = []

        async def publish(item):
            events.append(item)

        async def state_event(state, *, listening=None, turn_id=None):
            controller.state.terminal = state

        async def cancel_prewarm(_turn_id):
            return None

        controller._publish = publish
        controller._state_event = state_event
        controller._cancel_tts_prewarm = cancel_prewarm

        await controller._settle_committed_turn("turn_a", outcome="completed", speech_id="speech_a")
        await controller._settle_committed_turn("turn_a", outcome="completed", speech_id="speech_a")

        assert [item.type for item in events] == ["turn.latency", "turn.completed"]
        assert record.delivery == {"status": "completed", "termination": "completed"}
