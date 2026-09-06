from __future__ import annotations

import asyncio
from unittest.mock import patch
import unittest
from types import SimpleNamespace

import voice_terminal.controller.turn_controller as turn_controller_module
from voice_terminal.audio.capture import AudioCapture
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.providers.minimax_tts import MiniMaxTtsSession
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
    async def test_tts_failure_preserves_full_text_and_drains_long_audio(self):
        c = make_controller(capture=None, asr=FakeAsr(), vad=FakeVad())
        c._loop = asyncio.get_running_loop()
        c.player = PcmPlayer(sample_rate=32000, channels=1, output_device=None)
        session = MiniMaxTtsSession(SimpleNamespace())

        async def stream_chat(*_args, **_kwargs):
            yield "第一段回答内容很长。"
            yield "第二段仍然应当完整保留。"

        async def start():
            pass

        async def send(_text):
            session._store_audio(b"\x01\x00" * (32000 * 20))
            session._error = RuntimeError("synthetic close")
            session._finished.set()
            raise session._error

        session.start, session.send_text = start, send
        c.llm = SimpleNamespace(stream_chat=stream_chat)
        c.tts = SimpleNamespace(session=lambda: session)
        deadlines = []

        async def drain(timeout_seconds=10):
            deadlines.append(timeout_seconds)
            self.assertGreaterEqual(timeout_seconds, c.player.buffered_ms / 1000 + 30)
            c.player._callback(bytearray(32000 * 20 * 2), 32000 * 20, None, None)

        c.player.wait_until_empty = drain
        try:
            await c._begin_turn(initial_speech=True)
            response = await c._run_llm_and_tts(c.turns.current, "测试")
            self.assertEqual(response, "第一段回答内容很长。第二段仍然应当完整保留。")
            self.assertEqual(c.player.played_ms, 20000)
            self.assertEqual(c.state.current["tts_status"], "DEGRADED")
            self.assertTrue(deadlines)
            self.assertIsNone(session._audio)
        finally:
            await c.close()

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
