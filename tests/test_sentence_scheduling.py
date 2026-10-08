from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.controller.speech_pipeline import AdmissionState
from voice_terminal.config import TtsConfig, load_config
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
    assert config.tts.sentence_prefetch_ms == 3000
    assert config.tts.sentence_tick_ms == 50
    assert config.tts.sentence_min_effective_chars == 10
    assert config.tts.first_clause_timeout_ms == 400
    assert config.tts.subtitle_enable is False


def test_lock_block_minimum_is_loaded_and_passed_to_tts_client(tmp_path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tts:\n  sentence_min_effective_chars: 14\n", encoding="utf-8")
    config = load_config(Path("config/default.yaml"), local)
    client = MiniMaxTtsClient(
        base_url="https://example.invalid", websocket_path="/ws",
        model="speech-2.8-hd", api_key="test",
        sentence_min_effective_chars=config.tts.sentence_min_effective_chars,
    )
    assert client.sentence_min_effective_chars == 14


@pytest.mark.parametrize("minimum", [0, -1])
def test_lock_block_minimum_rejects_nonpositive_configuration(minimum) -> None:
    with pytest.raises(ValueError, match="sentence_min_effective_chars"):
        TtsConfig(sentence_min_effective_chars=minimum)
    with pytest.raises(ValueError, match="sentence_min_effective_chars"):
        MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws",
            model="speech-2.8-hd", api_key="test",
            sentence_min_effective_chars=minimum,
        )


def test_lock_block_counts_content_and_preserves_punctuation_at_exact_minimum() -> None:
    """恰好 10 个有效字符可提交，标点、空白和符号不提前凑齐下限。"""
    state = AdmissionState(lead_ms=0, sentence_window=True)
    segments = [" 你好，", "？！🦊 \n", "abc123。"]
    for segment in segments:
        state.push(segment)
    assert state.pop_sentence_block(min_effective_chars=10) is None
    assert list(state.staged) == segments
    state.push("甲乙。")
    state.push("尾巴。")
    assert state.pop_sentence_block(min_effective_chars=10) == "".join(segments) + "甲乙。"
    assert list(state.staged) == ["尾巴。"]
    assert state.staged_chars == len("尾巴。")
    assert state.sent_chars == 0  # 取候选块不等于语音合成已接受。


def test_incomplete_lock_block_remains_visible_as_revisable_tail() -> None:
    state = AdmissionState(lead_ms=0, sentence_window=True, restage=split_complete_text)
    locked = "前文已经完整送出。"
    state.commit_segment(locked, receipt_index=0)
    state.push("旧的，")
    state.push("内容。")
    assert state.pop_sentence_block(min_effective_chars=10) is None
    parts, valid = state.model_text_parts(locked + "旧的，内容。")
    assert valid is True
    assert parts["current_clause"] == locked
    assert parts["revisable_tail"] == "旧的，内容。"

    replacement = "新的完整内容，可以正常播报。"
    assert state.replace_unsent(replacement) == len("旧的，内容。")
    assert state.pop_sentence_block(min_effective_chars=10) == replacement
    assert state.sent_chars == len(locked)


def test_short_final_lock_block_is_released_only_when_generation_finishes() -> None:
    state = AdmissionState(lead_ms=0, sentence_window=True)
    state.push("好的。")
    assert state.pop_sentence_block(min_effective_chars=10) is None
    assert state.pop_sentence_block(min_effective_chars=10, allow_short=True) == "好的。"
    assert state.staged_chars == 0


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


def test_admission_projects_each_character_once_into_fixed_text_parts() -> None:
    state = AdmissionState(lead_ms=0, sentence_window=True)
    state.commit_segment("更早内容。", receipt_index=0)
    state.complete_segment(TtsSegmentReceipt(0, len("更早内容。"), 2000))
    state.commit_segment("当前短句。", receipt_index=1)
    state.complete_segment(TtsSegmentReceipt(1, len("当前短句。"), 6000))
    state.commit_segment("预取短句。", receipt_index=2)
    text = "更早内容。当前短句。预取短句。本地尾巴。"

    parts, valid = state.model_text_parts(
        text, output_estimated_ms=2500, bytes_per_second=1000,
    )

    assert valid is True
    assert parts == {
        "committed_prefix": "更早内容。",
        "current_clause": "当前短句。",
        "prefetched_clause": "预取短句。",
        "revisable_tail": "本地尾巴。",
    }
    assert "".join(parts.values()) == text
    assert (
        parts["committed_prefix"] + parts["current_clause"] + parts["prefetched_clause"]
        == text[:state.sent_chars]
    )


def test_model_speak_projection_uses_parts_without_legacy_text_fields() -> None:
    text = "更早内容。当前短句。预取短句。本地尾巴。"
    locked = "更早内容。当前短句。预取短句。"
    snapshot = {
        "queue_status": "speaking",
        "conversation_id": "conversation",
        "turn_id": "turn",
        "text": text,
        "sent_chars": len(locked),
        "rewritable": True,
        "_model_text_parts_valid": True,
        "_model_text_parts": {
            "committed_prefix": "更早内容。",
            "current_clause": "当前短句。",
            "prefetched_clause": "预取短句。",
            "revisable_tail": "本地尾巴。",
        },
        "speech_id": "speech",
        "delivery": {"speech_id": "speech", "heard_text": "独立传达事实。"},
    }

    projected = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="conversation", turn_id="turn",
    )

    assert projected == {
        "state": "speaking",
        "owner_scope": "current_turn",
        "continuation_mode": "rewrite_tail",
        "text_parts": snapshot["_model_text_parts"],
        "delivery": {"heard_text": "独立传达事实。"},
    }
    serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    for unique in ("更早内容。", "当前短句。", "预取短句。", "本地尾巴。"):
        assert serialized.count(unique) == 1
    for removed in ("target_text", "observed_text", "continuation_allowed", "sent_chars"):
        assert removed not in serialized


def _sentence_output_harness(*, min_effective_chars=10):
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
        sentence_min_effective_chars = min_effective_chars
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
        output, handle, session, player = _sentence_output_harness(min_effective_chars=1)
        handle.text = "第一句。第二句。第三句。"

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
        parts, valid = handle.admission.model_text_parts(
            handle.text, output_estimated_ms=0, bytes_per_second=2000,
        )
        assert valid is True
        assert parts == {
            "committed_prefix": "", "current_clause": "第一句。",
            "prefetched_clause": "", "revisable_tail": "第二句。第三句。",
        }

        player.played_ms = 1000  # first block has exactly 2000 ms left
        await asyncio.sleep(0.08)
        assert session.sent == ["第一句。", "第二句。"]
        parts, valid = handle.admission.model_text_parts(
            handle.text, output_estimated_ms=1000, bytes_per_second=2000,
        )
        assert valid is True
        assert parts == {
            "committed_prefix": "", "current_clause": "第一句。",
            "prefetched_clause": "第二句。", "revisable_tail": "第三句。",
        }

        player.played_ms = 3000  # second is current, but still has 3000 ms left
        await asyncio.sleep(0.08)
        assert session.sent == ["第一句。", "第二句。"]

        player.played_ms = 4000
        await asyncio.wait_for(running, 2)
        assert session.sent == ["第一句。", "第二句。", "第三句。"]
        assert handle.admission.sent_chars == len("第一句。第二句。第三句。")

    asyncio.run(scenario())


def test_sentence_window_merges_lock_blocks_and_projects_the_same_model_frontier() -> None:
    """合并块仍只占当前或预取的一个位置，第三块保持可改。"""
    async def scenario():
        output, handle, session, player = _sentence_output_harness()
        first = "明白，老大。我这边一切正常，"
        second = "运行稳定，随时听你调度。"
        third = "这些剩余内容，暂时保留可改。"
        handle.text = first + second + third
        handle.finished = True

        async def deltas():
            for segment in split_complete_text(handle.text):
                yield segment
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            await asyncio.sleep(0.08)
            assert session.sent == [first]
            player.played_ms = 1000
            await asyncio.sleep(0.08)
            assert session.sent == [first, second]
            snapshot = output.speak_snapshot
            projected = AgentEpisodeRuntime._model_speak_state(
                snapshot, conversation_id="episode", turn_id="turn",
            )
            assert projected["text_parts"] == {
                "committed_prefix": "", "current_clause": first,
                "prefetched_clause": second, "revisable_tail": third,
            }
            assert snapshot["sent_chars"] == len(first + second)
            player.played_ms = 4000
            await asyncio.wait_for(running, 2)
            assert session.sent == [first, second, third]
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_streaming_lock_block_waits_for_later_segments_without_advancing_prefix() -> None:
    async def scenario():
        output, handle, session, player = _sentence_output_harness()
        handle.text = "明白，老大。"
        short_ready = asyncio.Event()
        release = asyncio.Event()

        async def deltas():
            yield "明白，"
            yield "老大。"
            short_ready.set()
            await release.wait()
            handle.text += "我这边一切正常。"
            yield "我这边一切正常。"
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            await asyncio.wait_for(short_ready.wait(), 1)
            await asyncio.sleep(0)
            assert session.sent == []
            snapshot = output.speak_snapshot
            assert snapshot["sent_chars"] == 0
            assert snapshot["_model_text_parts"]["current_clause"] == ""
            assert snapshot["_model_text_parts"]["revisable_tail"] == handle.text
            player.played_ms = 1000  # 允许固定 3 秒测试音频的下一次窗口检查完成。
            release.set()
            await asyncio.wait_for(running, 2)
            assert session.sent == ["明白，老大。我这边一切正常。"]
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_revision_rejects_short_segments_before_staging_finishes() -> None:
    async def scenario():
        output, handle, session, player = _sentence_output_harness()
        handle.text = "旧的，内容。"
        handle.finished = True
        short_ready = asyncio.Event()
        release = asyncio.Event()

        async def deltas():
            yield "旧的，内容。"
            short_ready.set()
            await release.wait()
            yield None

        running = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ))
        try:
            await asyncio.wait_for(short_ready.wait(), 1)
            await asyncio.sleep(0)
            replacement = "新的完整内容，可以正常播报。"
            snapshot = output.speak_snapshot  # snapshot（原子修订的确认边界）。
            rewritten = await output.rewrite_unsent(
                ("episode", "revision"), "turn", speech_id=handle.speech_id,
                text=replacement,
                expected_generation=snapshot["output_generation"],
                expected_sent_chars=snapshot["sent_chars"], expected_text=snapshot["text"],
                expected_text_revision=snapshot.get("text_revision", 0),
            )
            # handle.finished（模型已结束）不代表本地分段已经确认完成。
            assert rewritten is None
            assert not handle.rewritable
            assert session.sent == []
            assert handle.admission.sent_chars == 0
            player.played_ms = 1000
            release.set()
            await asyncio.wait_for(running, 2)
            assert handle.text == "旧的，内容。"
            assert session.sent == ["旧的，内容。"]
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_short_answer_and_short_final_tail_do_not_wait_for_more_text() -> None:
    async def scenario(text, expected):
        output, handle, session, player = _sentence_output_harness()
        handle.text = text
        player.played_ms = 10 ** 9

        async def deltas():
            yield text
            yield None

        await asyncio.wait_for(output._play_stream(
            handle.workspace, handle.workspace.token, deltas(),
            speech_id=handle.speech_id, handle=handle,
        ), 2)
        assert session.sent == expected
        assert handle.admission.sent_chars == len(text)

    asyncio.run(scenario("好的。", ["好的。"]))
    first = "明白，老大。我这边一切正常，"
    asyncio.run(scenario(first + "谢谢。", [first, "谢谢。"]))


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
