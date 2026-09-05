from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
import unittest

from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
from voice_terminal.analysis.sensevoice import parse_transcript, bounded_segments
from voice_terminal.agent.runtime import AgentRuntime, ToolCallAssembler, SpokenContentGuard
from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.agent.speculative_tools import SpeculativeTools
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.controller.options import V02Options
from voice_terminal.controller.turn_workspace import AudioFrame, CurrentTurnAudioBuffer
from voice_terminal.controller.transcript_segmenter import TranscriptSegmenter
from voice_terminal.controller.agent_scheduler import PredictionScheduler
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.core.state import RuntimeState
from voice_terminal.core.event_bus import EventBus
from voice_terminal.core.events import event
from voice_terminal.server.history import InMemoryHistory
from voice_terminal.vad.detector import VadActivity


class FakeVad:
    def __init__(self):
        self.activity = VadActivity(False, False, False)
    def feed(self, pcm):
        result = self.activity
        self.activity = VadActivity(result.is_speech, False, False)
        return result
    def reset(self):
        self.activity = VadActivity(False, False, False)


class FakeAsr:
    connected = True
    on_result = None
    async def send_audio(self, chunk):
        pass
    async def send_finish(self, turn_id):
        pass
    async def close(self):
        pass


class HealthAsr(FakeAsr):
    def __init__(self, *, connected: bool = False, error: Exception | None = None) -> None:
        self.connected = connected
        self.error = error
        self.connect_calls = 0
        self.health_calls = 0

    async def connect(self):
        self.connect_calls += 1
        self.connected = True

    async def health(self):
        self.health_calls += 1
        if self.error is not None:
            raise self.error
        return {"type": "health", "status": "READY", "model_loaded": True}


class FakeProvider:
    def __init__(self):
        self.requests = []
        self.wait = None
    async def stream_events(self, messages, **kwargs):
        self.requests.append(messages)
        if self.wait:
            await self.wait.wait()
        yield {"type": "content", "text": "已理解。"}
        yield {"type": "finish", "reason": "stop"}


class FailingTtsSession:
    async def start(self):
        return None

    async def send_text(self, _text):
        raise RuntimeError("synthetic TTS transport close")

    async def finish(self):
        raise RuntimeError("synthetic TTS transport close")

    async def audio_chunks(self):
        if False:
            yield b""

    async def close(self):
        return None


class FailingTts:
    def session(self):
        return FailingTtsSession()


class PacingTtsSession:
    def __init__(self, player):
        self.player = player
        self.sent = []
        self.audio = asyncio.Queue()
        self.finished = False

    async def start(self):
        return None

    async def send_text(self, text):
        self.sent.append(text)
        # Each synthetic sentence is three seconds long.  The controller
        # must wait until the aggregate unplayed audio reaches the two-second
        # lead window before sending the next sentence.
        await self.audio.put(b"\x01\x00" * (32000 * 3))

    async def finish(self):
        self.finished = True
        await self.audio.put(None)

    async def audio_chunks(self):
        while True:
            item = await self.audio.get()
            if item is None:
                return
            yield item

    async def close(self):
        return None


class PacingTts:
    def __init__(self, player):
        self.session_instance = PacingTtsSession(player)

    def session(self):
        return self.session_instance


class FakeAnalysis:
    async def analyze(self, operation, pcm, turn, generation, timeout):
        if operation == "endpoint":
            return {"result": {"probability": .1}, "elapsed_ms": 1}
        return {"result": {"text": "最终校稿", "segments": [{"emotion": "ANGRY"}], "complete": True}, "elapsed_ms": 2}


def controller(**changes):
    asr_obj = changes.pop("asr_obj", FakeAsr())
    analysis_obj = changes.pop("analysis_obj", FakeAnalysis())
    options = replace(V02Options(enabled=True), **changes)
    return RealtimeController(options=options, analysis=analysis_obj, state=RuntimeState(), bus=EventBus(),
                              history=InMemoryHistory(), capture=None, kws=None, vad=FakeVad(), asr=asr_obj,
                              llm=FakeProvider(), tts=None, player=PcmPlayer(sample_rate=32000, channels=1, output_device=None),
                              wake_enabled=False, wake_keyword="hei claw", wake_preroll_ms=1200,
                              vad_min_speech_ms=250, vad_end_silence_ms=700, max_turn_ms=15000,
                              bridge_min_chars=8, bridge_max_chars=36, bridge_max_wait_ms=350, bridge_breaks="。！？；，、")


async def begin(c):
    c._loop = asyncio.get_running_loop()
    c.state.listening = True
    frame = AudioFrame(b"\x01\x00" * 16000, 0, time.monotonic())
    c._recent.append(frame)
    c._last_frame = frame
    c._speech_samples = 16000
    c._vad_origin = 0
    await c._new_turn(frame)
    await c._on_asr_result({"type": "asr.partial", "turn_id": c._sub_id, "text": "当前草稿"})
    return frame


async def settle(c):
    if c._completion_task:
        await asyncio.wait_for(asyncio.shield(c._completion_task), 2)


class PrimitiveTests(unittest.TestCase):
    def test_textual_tool_protocol_is_blocked_across_deltas(self):
        guard = SpokenContentGuard()
        self.assertEqual(guard.feed("我查一下。<"), "我查一下。")
        self.assertEqual(guard.feed("｜｜DS"), "")
        with self.assertRaises(ValueError):
            guard.feed("ML｜｜invoke name=\"files_read_text\">")

    def test_agent_runtime_never_yields_textual_tool_protocol(self):
        class Provider:
            async def stream_events(self, messages, **kwargs):
                yield {"type": "content", "text": "我正在查询<"}
                yield {"type": "content", "text": "｜｜DSML｜｜invoke"}
                yield {"type": "finish", "reason": "stop"}

        # The runtime is an async generator; execute it in a short helper
        # coroutine while keeping a real bounded tool registry.
        registry = SpeculativeTools(FileTools((Path.cwd() / "docs",)))
        try:
            runtime = AgentRuntime(Provider(), registry, tools_enabled=False)
            async def collect():
                return [piece async for piece in runtime.answer(
                    [], {"final_text": "查询", "final": True}, "turn", lambda payload: None
                )]
            pieces = asyncio.run(collect())
            spoken = "".join(piece for piece in pieces if piece)
            self.assertNotIn("DSML", spoken)
            self.assertIn("稍后重试", spoken)
        finally:
            registry.close()

    def test_sample_dedup_gap_and_bound(self):
        b = CurrentTurnAudioBuffer(4)
        b.append(AudioFrame(b"112233", 10, 0))
        b.append(AudioFrame(b"334455", 12, 0))
        self.assertEqual(bytes(b.pcm), b"22334455")
        with self.assertRaises(ValueError):
            b.append(AudioFrame(b"00", 16, 0))

        bounded = CurrentTurnAudioBuffer(4)
        bounded.append(AudioFrame(b"12345678", 0, 0))
        bounded.append(AudioFrame(b"90ab", 4, 0))
        self.assertEqual(bounded.start_sample, 2)
        self.assertEqual(bytes(bounded.pcm), b"567890ab")

    def test_segment_growth_correction_and_pause(self):
        s = TranscriptSegmenter(2, 4)
        self.assertIsNone(s.feed("查询文档"))
        self.assertEqual(s.feed("查询文档目录")["new_segment"], "查询文档")
        changed = s.feed("读取文档目录")
        self.assertTrue(changed["corrected"])
        self.assertEqual(changed["segments"], [])
        self.assertEqual(s.feed("读取文档目录", pause=True)["segments"], ["读取文档", "目录"])

    def test_audio_tags_and_bounded_long_segments(self):
        result = parse_transcript("<|zh|><|ANGRY|><|Speech|><|withitn|>你好")
        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["emotion"], "ANGRY")
        self.assertEqual(parse_transcript("<|OTHER|>text")["emotion"], "unknown")
        self.assertEqual(bounded_segments([[0, 60000]], 60000), [(0, 30000), (30000, 60000)])
        self.assertEqual(bounded_segments([[0, 30010]], 30010), [(0, 15005), (15005, 30010)])
        self.assertEqual(bounded_segments([[0, 45000]], 45000), [(0, 22500), (22500, 45000)])
        for start, end in bounded_segments([[0, 59800]], 59800):
            self.assertGreater(end - start, 10000)
            self.assertLessEqual(end - start, 30000)
        with self.assertRaises(ValueError):
            bounded_segments([[0, 10000], [9000, 11000]], 12000)

    def test_long_audio_matrix_has_bounded_ordered_segments(self):
        cases = {
            29800: [(0, 29800)],
            30000: [(0, 30000)],
            30010: [(0, 15005), (15005, 30010)],
            45000: [(0, 22500), (22500, 45000)],
            59800: [(0, 29900), (29900, 59800)],
            60000: [(0, 30000), (30000, 60000)],
            60800: [(0, 20267), (20267, 40534), (40534, 60800)],
            61000: [(0, 20334), (20334, 40668), (40668, 61000)],
        }
        for duration, expected in cases.items():
            actual = bounded_segments([[0, duration]], duration)
            self.assertEqual(actual, expected)
            self.assertTrue(all(0 < end - start <= 30000 for start, end in actual))
            self.assertTrue(all(actual[i][1] <= actual[i + 1][0] for i in range(len(actual) - 1)))

        # Ranges are clipped before splitting and short adjacent speech is
        # merged only when the resulting span stays within 15 seconds.
        self.assertEqual(bounded_segments([[-500, 4000], [5000, 12000]], 12000), [(0, 12000)])

    def test_tool_delta_assembly(self):
        a = ToolCallAssembler()
        a.feed({"index": 0, "id": "call1", "function": {"name": "files_search", "arguments": '{"que'}})
        a.feed({"index": 0, "function": {"arguments": 'ry":"规范"}'}})
        self.assertEqual(json.loads(a.complete()[0]["function"]["arguments"]), {"query": "规范"})
        a.feed({"index": 1, "function": {"arguments": "{}"}})
        with self.assertRaises(ValueError):
            a.complete()

    def test_playback_generation_gate(self):
        p = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
        p.allow("turn:1")
        p.enqueue("turn:1", b"11")
        self.assertFalse(p.block())
        p.enqueue("turn:1", b"22")
        p.allow("turn:2")
        p.enqueue("turn:1", b"33")
        p.enqueue("turn:2", b"44")
        output = bytearray(2)
        p._callback(output, 1, None, None)
        self.assertEqual(output, b"44")
        self.assertTrue(p.block())

    def test_tts_pacing_options_are_configurable(self):
        options = V02Options.from_mapping({"tts_pacing": True, "tts_lead_ms": 2000}, Path.cwd())
        self.assertTrue(options.tts_pacing)
        self.assertEqual(options.tts_lead_ms, 2000)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_transport_failure_does_not_truncate_completed_llm_text(self):
        c = controller()
        c.tts = FailingTts()
        try:
            frame = await begin(c)
            ws = c.workspace
            ws.final_text = "查询文档"
            ws.paraformer_final = ws.final_text
            ws.committed = True
            response = await c._answer(ws, ws.token)
            self.assertEqual(response, "已理解。")
            self.assertEqual(ws.staged_response, "已理解。")
            self.assertEqual(c.state.current["tts_status"], "DEGRADED")
        finally:
            await c.close()

    async def test_tts_sender_uses_total_remaining_audio_lead_window(self):
        c = controller(tts_pacing=True, tts_lead_ms=2000)
        c.tts = PacingTts(c.player)

        async def answer(*_args, **_kwargs):
            yield "第一段回答内容很长。"
            yield "第二段回答内容很长。"

        c.agent.answer = answer
        try:
            frame = await begin(c)
            ws = c.workspace
            ws.final_text = "查询文档"
            ws.paraformer_final = ws.final_text
            ws.committed = True
            task = asyncio.create_task(c._answer(ws, ws.token))

            async def wait_for_sent(count):
                for _ in range(100):
                    if len(c.tts.session_instance.sent) >= count:
                        return
                    await asyncio.sleep(.01)
                self.fail(f"expected {count} TTS segments, got {c.tts.session_instance.sent!r}")

            async def wait_for_buffer_at_least(value):
                for _ in range(100):
                    if c.player.buffered_ms >= value:
                        return
                    await asyncio.sleep(.01)
                self.fail(f"expected at least {value}ms queued, got {c.player.buffered_ms}ms")

            async def wait_for_remaining_at_least(value):
                for _ in range(100):
                    if c.state.current["tts_remaining_ms"] >= value:
                        return
                    await asyncio.sleep(.01)
                self.fail(
                    "expected at least "
                    f"{value}ms reported remaining audio, got "
                    f"{c.state.current['tts_remaining_ms']}ms"
                )

            await wait_for_sent(1)
            await wait_for_buffer_at_least(3000)
            await wait_for_remaining_at_least(2000)
            self.assertEqual(c.tts.session_instance.sent, ["第一段回答内容很长。"])
            self.assertEqual(c.player.buffered_ms, 3000)
            self.assertGreater(c.state.current["tts_remaining_ms"], 2000)

            # Consume 1.1 seconds from the aggregate queue.  The next text is
            # released only after the total remaining audio crosses 2 seconds.
            c.player._callback(bytearray(32000 * 2 * 1100 // 1000), 32000 * 1100 // 1000, None, None)
            await wait_for_sent(2)
            await wait_for_buffer_at_least(4900)
            self.assertEqual(c.tts.session_instance.sent, ["第一段回答内容很长。", "第二段回答内容很长。"])

            # Drain both synthetic sentences so _answer can finish cleanly.
            remaining = c.player.buffered_ms
            c.player._callback(bytearray(32000 * 2 * remaining // 1000 + 2), 32000 * remaining // 1000 + 1, None, None)
            await asyncio.wait_for(task, 2)
            self.assertTrue(c.tts.session_instance.finished)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await c.close()

    async def test_scheduler_coalesces_and_final_can_cancel(self):
        gate = asyncio.Event()
        calls = []
        async def run(snapshot):
            calls.append(snapshot)
            await gate.wait()
        s = PredictionScheduler(run, 1)
        s.submit({"revision": 1})
        await asyncio.sleep(.01)
        for n in range(2, 20):
            s.submit({"revision": n})
        self.assertEqual(len(calls), 1)
        gate.set()
        await asyncio.sleep(.02)
        self.assertEqual([c["revision"] for c in calls], [1, 19])
        s.stop()

    async def test_native_analysis_slot_survives_request_cancellation(self):
        gate = threading.Event()
        lane = AnalysisLane(lambda pcm: gate.wait(2) or {})
        task = asyncio.create_task(lane.run(b"00"))
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await lane.run(b"00")
        gate.set()
        await asyncio.shield(lane.active)
        lane.close()

    def test_analysis_health_reports_model_identity_and_loaded_state(self):
        ready = AnalysisLane(lambda _pcm: {}, model_name="SenseVoiceSmall")
        failed = AnalysisLane(error="model load failed", model_name="SenseVoiceSmall")
        try:
            self.assertEqual(ready.health_payload(), {
                "status": "READY",
                "model": "SenseVoiceSmall",
                "model_loaded": True,
                "error": None,
            })
            self.assertEqual(failed.health_payload(), {
                "status": "DEGRADED",
                "model": "SenseVoiceSmall",
                "model_loaded": False,
                "error": "model load failed",
            })
        finally:
            ready.close()
            failed.close()

    async def test_monitor_backpressure_never_blocks(self):
        bus = EventBus()
        stream = bus.subscribe()
        first = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await bus.publish(event("first"))
        await first
        for _ in range(257):
            await asyncio.wait_for(bus.publish(event("partial")), .1)
        with self.assertRaises(RuntimeError):
            await anext(stream)

    async def test_files_reuse_change_escape_and_limits(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "docs"
            root.mkdir()
            path = root / "sample.md"
            path.write_text("a" * 17000, encoding="utf-8")
            outside = Path(name) / "private.md"
            outside.write_text("secret")
            files = FileTools((root,))
            with self.assertRaises(PermissionError):
                files.execute("files_read_text", {"path": "../private.md"})
            tools = SpeculativeTools(files)
            try:
                first, reused = await tools.select("turn", "files_read_text", {"path": "sample.md"})
                result = await first.task
                self.assertTrue(result["truncated"])
                self.assertEqual(len(result["text"]), 16384)
                second, reused = await tools.select("turn", "files_read_text", {"path": str(path)})
                self.assertIs(first, second)
                self.assertTrue(reused)
                path.write_text("changed", encoding="utf-8")
                third, reused = await tools.select("turn", "files_read_text", {"path": str(path)})
                self.assertFalse(reused)
                self.assertEqual((await third.task)["text"], "changed")
                search, _ = await tools.select("turn", "files_search", {"query": "sample"})
                await search.task
                same, reused = await tools.select("turn", "files_search", {"query": "SAMPLE"})
                self.assertIs(search, same)
                self.assertTrue(reused)
                (root / "sample2.md").write_text("new")
                changed, reused = await tools.select("turn", "files_search", {"query": "sample"})
                self.assertFalse(reused)
                self.assertEqual(len((await changed.task)["files"]), 2)
            finally:
                tools.close()

    async def test_refinement_commits_one_exchange(self):
        c = controller(sensevoice=True)
        try:
            frame = await begin(c)
            sub_id = c._sub_id
            await c._freeze("test", frame)
            await c._on_asr_result({"type": "asr.final", "turn_id": sub_id, "text": "草稿"})
            await settle(c)
            self.assertEqual(len(c.history.messages), 3)
            self.assertEqual(c.history.messages[-2]["content"], "最终校稿")
            request = c.llm.requests[-1]
            user = [m for m in request if m["role"] == "user"]
            self.assertEqual(len(user), 1)
            package = json.loads(user[0]["content"])
            self.assertEqual(package["audio_cues"][0]["emotion"], "ANGRY")
            self.assertEqual(package["speech_emotion"]["status"], "READY")
            self.assertEqual(package["speech_emotion"]["source"], "SenseVoiceSmall")
            self.assertEqual(package["speech_emotion"]["segments"][0]["emotion"], "ANGRY")
        finally:
            await c.close()

    async def test_refinement_failure_uses_paraformer(self):
        c = controller(sensevoice=True)
        async def fail(*args):
            raise TimeoutError()
        c.analysis.analyze = fail
        try:
            frame = await begin(c)
            await c._freeze("test", frame)
            await c._on_asr_result({"type": "asr.final", "turn_id": c._sub_id, "text": "最终草稿"})
            await settle(c)
            self.assertEqual(c.history.messages[-2]["content"], "最终草稿")
        finally:
            await c.close()

    async def test_valid_no_speech_skips_llm(self):
        c = controller(sensevoice=True)
        async def empty(*args):
            return {"result": {"text": "", "complete": True, "no_speech": True, "segments": []}, "elapsed_ms": 1}
        c.analysis.analyze = empty
        try:
            frame = await begin(c)
            await c._freeze("test", frame)
            await c._on_asr_result({"type": "asr.final", "turn_id": c._sub_id, "text": "幻觉"})
            await settle(c)
            self.assertEqual(c.llm.requests, [])
            self.assertEqual(len(c.history.messages), 1)
        finally:
            await c.close()

    async def test_multiple_resumes_keep_same_turn_and_drop_old_answers(self):
        c = controller()
        c.llm.wait = asyncio.Event()
        try:
            frame = await begin(c)
            turn = c.workspace.turn_id
            for _ in range(3):
                sub = c._sub_id
                await c._freeze("test", frame)
                await c._on_asr_result({"type": "asr.final", "turn_id": sub, "text": "条件"})
                await asyncio.sleep(.01)
                old = c.workspace.token
                await c._resume(frame)
                self.assertEqual(c.workspace.turn_id, turn)
                self.assertFalse(c._valid(old))
                self.assertEqual(len(c.history.messages), 1)
            c.llm.wait.set()
            await c._freeze("test", frame)
            await c._on_asr_result({"type": "asr.final", "turn_id": c._sub_id, "text": "结尾"})
            await settle(c)
            self.assertEqual(c.history.messages[-2]["content"], "条件条件条件结尾")
            self.assertEqual(len(c.history.messages), 3)
        finally:
            await c.close()

    async def test_endpoint_late_complete_ignored_after_speech_version_changes(self):
        c = controller(smart_turn=True)
        gate = asyncio.Event()
        async def endpoint(*args):
            await gate.wait()
            return {"result": {"probability": .99}, "elapsed_ms": 1}
        c.analysis.analyze = endpoint
        try:
            frame = await begin(c)
            ws = c.workspace
            task = asyncio.create_task(c._endpoint(ws, frame))
            await asyncio.sleep(0)
            ws.speech_version += 1
            gate.set()
            await task
            self.assertTrue(c._active_input)
            self.assertEqual(ws.generation_id, 0)
        finally:
            await c.close()

    async def test_800ms_incomplete_does_not_commit(self):
        c = controller(smart_turn=True)
        try:
            frame = await begin(c)
            c._speech_end_sample = 16000
            silence = AudioFrame(b"\x00\x00" * 12800, 16000, frame.captured_at + .8)
            await c.handle_frame(silence)
            await c._endpoint_task
            self.assertTrue(c._active_input)
            self.assertFalse(c.workspace.committed)
        finally:
            await c.close()

    async def test_final_does_not_wait_for_prediction(self):
        c = controller(predictions=True)
        gate = asyncio.Event()
        async def predict(*args):
            await gate.wait()
        c.agent.predict = predict
        try:
            frame = await begin(c)
            c.scheduler.submit({"turn_id": c.workspace.turn_id, "generation_id": 0, "input_revision": 0})
            await asyncio.sleep(.01)
            await c._freeze("test", frame)
            await c._on_asr_result({"type": "asr.final", "turn_id": c._sub_id, "text": "最终"})
            await settle(c)
            self.assertEqual(len(c.llm.requests), 1)
            self.assertLess(c.history.records[0].latency["commit_to_final_llm_ms"], 50)
        finally:
            await c.close()

    async def test_commit_to_final_llm_local_schedule_p95(self):
        c = controller()
        try:
            await begin(c)
            samples = []
            for _ in range(100):
                c.workspace.timings["commit"] = time.monotonic()
                await asyncio.sleep(0)
                c.workspace.timings["llm_request"] = time.monotonic()
                await c._latency(c.workspace)
                samples.append(c.state.latency["values"]["commit_to_final_llm_ms"])
            p95 = statistics.quantiles(samples, n=20, method="inclusive")[18]
            self.assertLessEqual(p95, 50)
        finally:
            await c.close()

    async def test_hard_limit_continuous_speech_starts_new_turn(self):
        c = controller(max_turn_ms=1000)
        try:
            frame = await begin(c)
            turn = c.workspace.turn_id
            c._speech_start_sample = 0
            c.vad.activity = VadActivity(True, False, False)
            next_frame = AudioFrame(b"11" * 16000, 16000, frame.captured_at + 1)
            await c.handle_frame(next_frame)
            self.assertTrue(c.workspace.hard_closed)
            continuation = AudioFrame(b"22" * 4800, 32000, frame.captured_at + 1.3)
            await c.handle_frame(continuation)
            self.assertNotEqual(c.workspace.turn_id, turn)
            self.assertEqual(c.workspace.audio.start_sample, 32000)
            self.assertEqual(bytes(c.workspace.audio.pcm), continuation.pcm)
        finally:
            await c.close()

    async def test_sixty_second_limit_closes_and_restarts_after_continuation(self):
        c = controller(max_turn_ms=60000)
        stream = None
        try:
            frame = await begin(c)
            turn = c.workspace.turn_id
            stream = c.bus.subscribe()
            first_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            c._speech_start_sample = 0
            c.vad.activity = VadActivity(True, False, False)
            # Keep the sample stream contiguous while jumping the captured
            # timestamp to the 60-second runtime limit.
            long_frame = AudioFrame(b"11" * 944000, 16000, frame.captured_at + 60.0)
            await c.handle_frame(long_frame)
            self.assertTrue(c.workspace.hard_closed)
            events = [await asyncio.wait_for(first_event, 1), await asyncio.wait_for(anext(stream), 1)]
            finalizing = next(item for item in events if item.type == "turn.finalizing")
            self.assertEqual(finalizing.payload["reason"], "max_duration")
            continuation = AudioFrame(b"22" * 4800, 960000, frame.captured_at + 60.3)
            await c.handle_frame(continuation)
            self.assertNotEqual(c.workspace.turn_id, turn)
            self.assertEqual(c.workspace.audio.start_sample, 960000)
            self.assertEqual(bytes(c.workspace.audio.pcm), continuation.pcm)
        finally:
            if stream is not None:
                await stream.aclose()
            await c.close()

    async def test_asr_disconnect_is_recoverable_and_restart_health_is_required(self):
        asr = HealthAsr(connected=True)
        c = controller(asr_obj=asr)
        stream = None
        try:
            await begin(c)
            stream = c.bus.subscribe()
            next_event = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            await c._on_asr_result({
                "type": "error",
                "code": "ASR_WORKER_DISCONNECTED",
                "message": "socket closed",
            })
            first = await asyncio.wait_for(next_event, 1)
            self.assertEqual(first.type, "error")
            self.assertEqual(first.payload["code"], "ASR_WORKER_DISCONNECTED")
            self.assertTrue(first.payload["recoverable"])
            self.assertIsNone(c.workspace)
            self.assertEqual(c.state.services["asr"], "ERROR")
            self.assertFalse(c.state.listening)
            self.assertEqual(c._asr_queue.qsize(), 0)

            asr.connected = False
            self.assertTrue(await c._ensure_asr_ready())
            self.assertEqual(asr.connect_calls, 1)
            self.assertEqual(asr.health_calls, 1)
            self.assertEqual(c.state.services["asr"], "READY")
        finally:
            if stream is not None:
                await stream.aclose()
            await c.close()

    async def test_actual_playback_boundary_forces_new_turn(self):
        c = controller()
        try:
            frame = await begin(c)
            old = c.workspace.turn_id
            await c._freeze("test", frame)
            c.workspace.committed = True
            c.player.allow(c.workspace.playback_key)
            c.player.enqueue(c.workspace.playback_key, b"11")
            c.player._callback(bytearray(2), 1, None, None)
            c._resume_started = 16000
            c.vad.activity = VadActivity(True, False, False)
            await c.handle_frame(AudioFrame(b"22" * 4800, 16000, frame.captured_at + .3))
            self.assertNotEqual(c.workspace.turn_id, old)
        finally:
            await c.close()

    async def test_post_freeze_silence_does_not_overflow_workspace_audio(self):
        c = controller()
        try:
            frame = await begin(c)
            await c._freeze("test", frame)
            for index in range(400):
                await c.handle_frame(AudioFrame(b"\x00\x00" * 1600, frame.end_sample + index * 1600, frame.captured_at + index * .1))
            self.assertLessEqual(len(c.workspace.audio.pcm) // 2, c.workspace.audio.max_samples)
            self.assertEqual(c.workspace.audio.end_sample, frame.end_sample + 400 * 1600)
        finally:
            await c.close()

    async def test_stale_prediction_cannot_launch_tool(self):
        c = controller(predictions=True, tools=True)
        gate = asyncio.Event()
        async def predict(*args):
            await gate.wait()
            return {"decision": "ACT", "intent": "old", "tools": [{"name": "files_search", "arguments": {"query": "v0.2"}}]}
        c.agent.predict = predict
        try:
            await begin(c)
            ws = c.workspace
            task = asyncio.create_task(c._predict({"turn_id": ws.turn_id, "generation_id": 0, "input_revision": ws.input_revision}))
            await asyncio.sleep(0)
            ws.input_revision += 1
            gate.set()
            await task
            self.assertEqual(c.registry.entries, {})
            self.assertEqual(ws.prediction, {})
        finally:
            await c.close()

    async def test_prediction_selection_rechecks_revision_before_task_creation(self):
        c = controller(predictions=True, tools=True)
        normalize_started = threading.Event()
        release_normalize = threading.Event()
        original_normalize = c.registry.files.normalize

        def delayed_normalize(name, arguments):
            normalize_started.set()
            release_normalize.wait(2)
            return original_normalize(name, arguments)

        c.registry.files.normalize = delayed_normalize
        c.agent.predict = lambda *args, **kwargs: _one_result(
            {"decision": "ACT", "intent": "old", "tools": [{"name": "files_search", "arguments": {"query": "v0.2"}}]}
        )
        try:
            await begin(c)
            ws = c.workspace
            revision = ws.input_revision
            task = asyncio.create_task(c._predict({"turn_id": ws.turn_id, "generation_id": ws.generation_id,
                                                    "input_revision": revision, "text": "旧输入"}))
            await asyncio.to_thread(normalize_started.wait, 1)
            ws.input_revision += 1
            release_normalize.set()
            await task
            self.assertEqual(c.registry.entries, {})
            self.assertEqual(ws.prediction, {})
            self.assertIsNone(ws.prediction_revision)
        finally:
            release_normalize.set()
            await c.close()

    async def test_prediction_selection_rechecks_generation_during_freshness(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "docs"
            root.mkdir()
            path = root / "sample.md"
            path.write_text("stable", encoding="utf-8")
            files = FileTools((root,))
            tools = SpeculativeTools(files)
            try:
                first, _ = await tools.select("turn", "files_read_text", {"path": "sample.md"})
                await first.task
                entered = threading.Event()
                release = threading.Event()
                original_fresh = files.fresh

                def delayed_fresh(tool_name, result):
                    entered.set()
                    release.wait(2)
                    return original_fresh(tool_name, result)

                files.fresh = delayed_fresh
                valid = True

                def is_valid():
                    return valid

                selection = asyncio.create_task(tools.select("turn", "files_read_text", {"path": "sample.md"}, valid=is_valid))
                await asyncio.to_thread(entered.wait, 1)
                valid = False
                release.set()
                with self.assertRaisesRegex(RuntimeError, "invalidated"):
                    await selection
                self.assertEqual(len(tools.entries), 1)
            finally:
                release.set() if "release" in locals() else None
                tools.close()


def _one_result(value):
    async def result(*args, **kwargs):
        return value
    return result()


if __name__ == "__main__":
    unittest.main()
