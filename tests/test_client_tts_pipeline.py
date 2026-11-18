"""Client TTS direct playback（客户端直放流水线）回归：等待真实设备输出证据。"""
from __future__ import annotations

import asyncio
import time

from tests.test_sentence_scheduling import _sentence_output_harness


class _DirectSession:
    direct_playback = True
    bidi = True
    reusable = False
    sample_rate = 32_000

    def __init__(self, *, enough_output: bool = True) -> None:
        self.enough_output = enough_output
        self.sent: list[str] = []
        self.accepted = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed = False
        self.first_audio_ready_at = None
        self.progress = {
            "accepted_samples": 0, "played_samples": 0, "buffered_samples": 0,
            "output_complete": False, "device_time_us": 0,
        }

    async def start(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)
        self.accepted.set()

    async def wait_for_segment(self, index: int) -> dict[str, int]:
        await self.finished.wait()
        samples = 3_200 if self.enough_output else 100
        self.first_audio_ready_at = time.monotonic()
        return {
            "index": index, "text_chars": len(self.sent[index]),
            "end_received_bytes": samples * 2, "start_sample": 0, "end_sample": samples,
        }

    async def audio_chunks(self):
        raise AssertionError("direct playback must not request PCM from the server pipeline")
        yield b""

    def playback_progress(self):
        return dict(self.progress)

    async def flush(self) -> None:
        await self.finished.wait()

    async def wait_until_output_complete(self, *, timeout_seconds: float = 120.0):
        await asyncio.wait_for(self.finished.wait(), timeout=timeout_seconds)
        return dict(self.progress)

    async def close(self) -> None:
        self.closed = True


class _DirectClient:
    transport = "websocket_bidi"
    scheduling = "direct"
    effective_scheduling = "direct"
    direct_playback = True
    sample_rate = 32_000
    playback_startup_buffer_ms = 0
    playback_resume_buffer_ms = 0
    sentence_prefetch_ms = 3_000
    sentence_tick_ms = 50
    sentence_min_effective_chars = 10

    def __init__(self, session: _DirectSession) -> None:
        self.instance = session

    def session(self, **_kwargs) -> _DirectSession:
        return self.instance


def _pipe(*, enough_output: bool = True):
    output, handle, _old_session, player = _sentence_output_harness(min_effective_chars=1)
    player.diagnostics = {}
    output.controller.state = type("State", (), {"current": {}})()
    session = _DirectSession(enough_output=enough_output)
    client = _DirectClient(session)
    output.tts = client
    handle.tts_client = client
    handle.tts_backend = "minimax"
    handle.text = "客户端直放必须等待设备回执。"
    return output, handle, session, player


def test_direct_pipeline_does_not_read_pcm_or_finish_before_device_receipt() -> None:
    async def scenario():
        output, handle, session, player = _pipe()

        async def deltas():
            yield handle.text
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(), speech_id=handle.speech_id, handle=handle,
        ))
        try:
            await asyncio.wait_for(session.accepted.wait(), 2)
            await asyncio.sleep(0.02)
            assert not running.done()
            assert player.buffered_ms == 0
            assert session.closed is False
            session.progress.update({
                "accepted_samples": 3_200, "played_samples": 3_200,
                "buffered_samples": 0, "output_complete": True, "device_time_us": 12_000,
            })
            session.finished.set()
            await asyncio.wait_for(running, 2)
            assert session.sent == [handle.text]
            assert session.closed is True
            assert handle.workspace.timings["tts_audio_bytes"] == 0
            assert handle.workspace.timings["tts_client_output_samples"] == 3_200
            assert handle.workspace.timings["playback_output_complete"] is True
            assert handle.workspace.playback_started is True
            assert handle.snapshot["precision"] == "device_time"
            assert output.state.current["tts_status"] == "FINISHED"
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_direct_pipeline_rejects_short_device_output() -> None:
    async def scenario():
        output, handle, session, _player = _pipe(enough_output=False)

        async def deltas():
            yield handle.text
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(), speech_id=handle.speech_id, handle=handle,
        ))
        try:
            await asyncio.wait_for(session.accepted.wait(), 2)
            session.progress.update({
                "accepted_samples": 100, "played_samples": 100,
                "buffered_samples": 0, "output_complete": True, "device_time_us": 12_000,
            })
            session.finished.set()
            await asyncio.wait_for(running, 2)
            assert handle.workspace.timings["tts_audio_bytes"] == 0
            assert handle.workspace.timings["playback_output_complete"] is False
            assert output.state.current["tts_status"] == "DEGRADED"
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
