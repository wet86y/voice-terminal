from __future__ import annotations

import asyncio
from unittest.mock import patch
import unittest

import voice_terminal.controller.turn_controller as turn_controller_module
from voice_terminal.audio.capture import AudioCapture
from voice_terminal.controller.turn_controller import TurnController
from voice_terminal.core.event_bus import EventBus
from voice_terminal.core.state import RuntimeState, TerminalState
from voice_terminal.server.history import InMemoryHistory
from voice_terminal.vad.detector import VadActivity


class FakeCapture:
    def __init__(self) -> None:
        self.stopped = False
        self.reads = [b"\x01\x00" * 320]

    async def start(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def read(self) -> bytes:
        return self.reads.pop(0)

    def preroll(self, _milliseconds: int) -> bytes:
        return b""


class FakeVad:
    def __init__(self) -> None:
        self.activity = VadActivity(True, False, False)

    def reset(self) -> None:
        self.activity = VadActivity(True, False, False)

    def feed(self, _pcm: bytes) -> VadActivity:
        return self.activity


class FakeAsr:
    connected = True

    def __init__(self) -> None:
        self.fail_audio = False
        self.closed = False
        self.on_result = None

    async def connect(self) -> None:
        self.connected = True

    async def send_audio(self, _chunk) -> None:
        if self.fail_audio:
            raise RuntimeError("ASR transport failed")

    async def send_finish(self, _turn_id: str) -> None:
        return None

    async def close(self) -> None:
        self.connected = False
        self.closed = True


def make_controller(*, capture: FakeCapture | None, asr: FakeAsr, vad: FakeVad) -> TurnController:
    return TurnController(
        state=RuntimeState(),
        bus=EventBus(),
        history=InMemoryHistory(),
        capture=capture,
        kws=None,
        vad=vad,
        asr=asr,
        llm=None,
        tts=None,
        player=None,
        wake_enabled=False,
        wake_keyword="hei claw",
        wake_preroll_ms=1200,
        vad_min_speech_ms=250,
        vad_end_silence_ms=700,
        max_turn_ms=15000,
        bridge_min_chars=8,
        bridge_max_chars=36,
        bridge_max_wait_ms=350,
        bridge_breaks="。！？；，、",
    )


class TurnControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_asr_final_timeout_releases_turn_and_returns_to_idle(self) -> None:
        asr = FakeAsr()
        controller = make_controller(capture=None, asr=asr, vad=FakeVad())
        controller._loop = asyncio.get_running_loop()
        controller.state.listening = True
        await controller._begin_turn(initial_speech=True)
        turn_id = controller.turns.current
        assert turn_id is not None
        controller._turn_has_valid_speech[turn_id] = True
        controller._asr_latest_text[turn_id] = "未确认草稿"

        def immediate_timeout(awaitable, timeout=None):
            del timeout
            awaitable.close()

            async def raise_timeout():
                raise asyncio.TimeoutError

            return raise_timeout()

        with patch.object(turn_controller_module.asyncio, "wait_for", immediate_timeout):
            await controller._complete_turn(turn_id)

        self.assertIsNone(controller.turns.current)
        self.assertEqual(controller.state.state, TerminalState.IDLE)
        self.assertTrue(controller.state.listening)
        await controller.close()

    async def test_audio_pipeline_failure_stops_broken_session(self) -> None:
        capture = FakeCapture()
        asr = FakeAsr()
        controller = make_controller(capture=capture, asr=asr, vad=FakeVad())
        controller._loop = asyncio.get_running_loop()
        controller.state.listening = True
        await controller._begin_turn(initial_speech=True)
        asr.fail_audio = True

        await controller._listen_loop()

        self.assertIsNone(controller.turns.current)
        self.assertFalse(controller.state.listening)
        self.assertEqual(controller.state.state, TerminalState.IDLE)
        self.assertTrue(capture.stopped)
        self.assertTrue(asr.closed)
        await controller.close()


class AudioCaptureQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_drops_frames_queued_by_previous_session(self) -> None:
        capture = AudioCapture.__new__(AudioCapture)
        capture.queue = asyncio.Queue(maxsize=4)
        capture._stream = None
        capture._running = False
        capture._blocks_captured = 0
        capture._bytes_captured = 0
        capture._dropped_chunks = 0
        capture.last_error = None
        capture.queue.put_nowait(object())

        await capture.stop()

        self.assertTrue(capture.queue.empty())


if __name__ == "__main__":
    unittest.main()
