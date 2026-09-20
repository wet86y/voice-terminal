from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.controller.speech_pipeline import AdmissionState
from voice_terminal.config import load_config
from voice_terminal.providers.minimax_tts import MiniMaxTtsClient, MiniMaxTtsSession
from voice_terminal.tts.scheduling import TtsSegmentReceipt
from voice_terminal.tts.sentence_segmenter import (
    PunctuationSentenceSegmenter,
    split_complete_text,
)


def test_sentence_segmenter_preserves_text_and_spoken_punctuation() -> None:
    segmenter = PunctuationSentenceSegmenter()
    chunks = []
    chunks.extend(segmenter.feed("  你好，下一句?!再来：尾"))
    chunks.extend(segmenter.finish())
    assert chunks == ["  你好，", "下一句?!", "再来：", "尾"]
    assert "".join(chunks) == "  你好，下一句?!再来：尾"


def test_default_config_selects_standard_sentence_window() -> None:
    config = load_config(Path("config/default.yaml"))
    assert config.tts.transport == "websocket"
    assert config.tts.scheduling == "sentence_window"
    assert config.tts.sentence_prefetch_ms == 2000
    assert config.tts.sentence_tick_ms == 50
    assert config.tts.first_clause_timeout_ms == 400
    assert config.tts.subtitle_enable is False


def test_sentence_segmenter_keeps_consecutive_punctuation_and_ignores_blank_block() -> None:
    segmenter = PunctuationSentenceSegmenter()
    assert segmenter.feed("？！") == ["？！"]
    assert segmenter.feed("\n") == []
    assert segmenter.feed("下一句。") == ["\n下一句。"]


def test_first_clause_timeout_is_one_shot_and_not_character_based() -> None:
    segmenter = PunctuationSentenceSegmenter(first_clause_timeout_ms=400)
    assert segmenter.feed("没有标点但可以非常长", now=10.0) == []
    assert segmenter.flush_due(now=10.399) == []
    assert segmenter.flush_due(now=10.4) == ["没有标点但可以非常长"]
    assert segmenter.feed("后续仍然无标点", now=11.0) == []
    assert segmenter.flush_due(now=20.0) == []
    assert segmenter.finish() == ["后续仍然无标点"]


def test_rewrite_restages_complete_sentence_tail() -> None:
    state = AdmissionState(lead_ms=0, restage=split_complete_text)
    state.sent_chars = len("已发送。")
    state.push("旧尾巴。")
    state.replace_unsent("新尾巴，第二句。末尾")
    assert list(state.staged) == ["新尾巴，", "第二句。", "末尾"]
    assert state.sent_chars == len("已发送。")


def _sentence_output_harness():
    class Session:
        bidi = False
        reusable = False
        connection_ready = True
        generation_finished = True

        def __init__(self):
            self.received_bytes = 0
            self.sent: list[str] = []
            self.receipts: list[TtsSegmentReceipt] = []
            self.audio = asyncio.Queue()
            self.receipt_event = asyncio.Event()
            self.completed_segments = 0

        async def start(self):
            return None

        async def send_text(self, text):
            self.sent.append(text)
            index = len(self.sent) - 1

            async def synthesize():
                await asyncio.sleep(0.01)
                pcm = b"\0" * 6000  # 3000 ms at the harness format.
                self.received_bytes += len(pcm)
                await self.audio.put(pcm)
                self.receipts.append(TtsSegmentReceipt(index, len(text), self.received_bytes))
                self.completed_segments = len(self.receipts)
                self.receipt_event.set()

            asyncio.create_task(synthesize())

        async def wait_for_segment(self, previous_index):
            while len(self.receipts) <= previous_index:
                self.receipt_event.clear()
                await self.receipt_event.wait()
            return self.receipts[previous_index]

        async def audio_chunks(self):
            while True:
                item = await self.audio.get()
                if item is None:
                    return
                yield item

        async def finish(self):
            await self.audio.put(None)

        async def close(self):
            return None

    class Tts:
        transport = "websocket"
        effective_scheduling = "sentence_window"
        sentence_prefetch_ms = 2000
        sentence_tick_ms = 10
        first_clause_timeout_ms = 400

        def __init__(self):
            self.session_instance = Session()

        def session(self, **_kwargs):
            return self.session_instance

    class Player:
        sample_rate = 1000
        channels = 1

        def __init__(self):
            self.played_ms = 0

        @property
        def buffered_ms(self):
            return 0

        def allow(self, _key):
            return None

        def enqueue(self, _key, _pcm):
            return None

        def progress(self, key):
            return {
                "key": key,
                "played_ms": self.played_ms,
                "output_estimated_ms": self.played_ms,
                "buffered_ms": 0,
                "started": True,
            }

        async def wait_until_output_complete(self, timeout_seconds=10):
            return None

    async def noop(*_args, **_kwargs):
        return None

    tts = Tts()
    player = Player()
    controller = SimpleNamespace(
        tts=tts,
        player=player,
        options=SimpleNamespace(tts_pacing=False, tts_lead_ms=1, tts_local_buffer_ms=1),
        bridge_options={},
        _turn_timings={},
        _track=lambda task: task,
    )
    output = EpisodeSpeechOutput(controller)
    output.runtime = SimpleNamespace(
        gate=lambda _identity: None,
        emit=noop,
        log=noop,
        user_speaking=False,
        closed=False,
        request_started_at=None,
    )
    output.loop = asyncio.get_running_loop()
    handle = SpeechHandle(output, ("episode", "request"), "turn", 0, "calm")
    handle.task = asyncio.current_task()
    output.handle = handle
    output._handles_by_token[handle.workspace.token] = handle
    return output, handle, tts.session_instance, player


def test_sentence_window_keeps_only_current_and_one_prefetched_block() -> None:
    async def scenario():
        output, handle, session, player = _sentence_output_harness()

        async def deltas():
            yield "第一句。第二句。第三句。"
            yield None

        running = asyncio.create_task(
            output._play_stream(
                handle.workspace,
                handle.workspace.token,
                deltas(),
                speech_id=handle.speech_id,
                handle=handle,
            )
        )
        await asyncio.sleep(0.08)
        assert session.sent == ["第一句。"]

        player.played_ms = 1000  # first block has exactly 2000 ms left
        await asyncio.sleep(0.08)
        assert session.sent == ["第一句。", "第二句。"]

        player.played_ms = 3000  # second is current, but still has 3000 ms left
        await asyncio.sleep(0.08)
        assert session.sent == ["第一句。", "第二句。"]

        player.played_ms = 4000
        await asyncio.wait_for(running, 2)
        assert session.sent == ["第一句。", "第二句。", "第三句。"]
        assert handle.admission.sent_chars == len("第一句。第二句。第三句。")

    asyncio.run(scenario())


def test_standard_websocket_prewarm_is_turn_scoped_and_start_is_not_repeated() -> None:
    async def scenario():
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws/v1/t2a_v2",
            model="speech-2.8-hd",
            api_key="test",
            transport="websocket",
            emotion_mode="off",
        )
        events: asyncio.Queue[dict] = asyncio.Queue()
        await events.put({"event": "task_started"})

        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, payload):
                self.sent.append(json.loads(payload))

            async def recv(self):
                return json.dumps(await events.get())

            async def close(self):
                return None

        socket = Socket()

        async def connect(*_args, **_kwargs):
            return socket

        try:
            with patch("websockets.connect", new=connect):
                result = await client.prewarm(turn_id="turn-a")
                session = client.session(turn_id="turn-a")
                assert result["status"] == "READY"
                assert client.session(turn_id="turn-b") is not session
                assert client.checkout_prewarm(session, turn_id="turn-b") is False
                assert client.checkout_prewarm(session, turn_id="turn-a") is True
                await session.start()
            assert [item["event"] for item in socket.sent] == ["task_start"]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_standard_is_final_receipts_are_ordered_cumulative_boundaries() -> None:
    async def scenario():
        class Socket:
            def __init__(self):
                self.events = [
                    {"data": {"audio": "0001"}, "is_final": True},
                    {"data": {"audio": "020304"}},
                    {"is_final": True},
                    {"event": "task_finished"},
                ]

            async def recv(self):
                return json.dumps(self.events.pop(0))

            async def close(self):
                return None

        session = MiniMaxTtsSession(SimpleNamespace(receive_timeout_seconds=1))
        session._socket = Socket()
        session._pending_segment_chars.extend([3, 5])
        await session._receive_loop()
        first = await session.wait_for_segment(0)
        second = await session.wait_for_segment(1)
        assert first == TtsSegmentReceipt(index=0, text_chars=3, end_received_bytes=2)
        assert second == TtsSegmentReceipt(index=1, text_chars=5, end_received_bytes=5)
        await session.close()

    asyncio.run(scenario())
