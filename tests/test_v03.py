import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from voice_terminal.agent.episode import AgentEpisodeRuntime, semantic_text
from voice_terminal.agent.options import V03Options
from voice_terminal.agent.speech_stream import SpeechArgumentStream
from voice_terminal.agent.speculative_tools import SpeculativeTools
from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.options import V02Options
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.core.state import RuntimeState
from voice_terminal.core.event_bus import EventBus
from voice_terminal.server.history import InMemoryHistory
from voice_terminal.audio.player import PcmPlayer
from tests.test_v02 import FakeVad, FakeAsr, FakeAnalysis, PacingTts, PacingTtsSession


def call(name, arguments, index=0):
    return {"type": "tool_delta", "call": {"index": index, "id": f"call_{index}",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}}


class Provider:
    def __init__(self, responses=None):
        self.responses = list(responses or [[call("wait_for_input", {})]])
        self.messages = []
        self.active = self.maximum = 0
        self.hold = None

    async def stream_events(self, messages, **kwargs):
        self.messages.append(json.loads(json.dumps(messages)))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if self.hold:
                await self.hold.wait()
            response = self.responses.pop(0) if self.responses else [call("wait_for_input", {})]
            for item in response:
                yield item
                await asyncio.sleep(0)
            yield {"type": "finish", "reason": "tool_calls"}
        finally:
            self.active -= 1


class Output:
    active = False
    status = "IDLE"

    def __init__(self):
        self.started = []
        self.texts = []
        self.invalidations = []

    def invalidate(self, reason):
        self.invalidations.append(reason)

    async def begin(self, identity, turn_id):
        self.started.append(identity)
        owner = self
        class Handle:
            speech_id = "speech"
            async def feed(self, text):
                owner.texts.append(text)
            async def finish(self):
                pass
        return Handle()

    async def close(self):
        pass


async def idle(runtime):
    for _ in range(500):
        if not runtime.pending and runtime.active_request is None and runtime.queue.empty():
            return
        await asyncio.sleep(.002)
    raise AssertionError("runtime did not become idle")


class ParserTests(unittest.TestCase):
    def test_semantic_normalization_keeps_numeric_and_identifier_punctuation(self):
        self.assertEqual(semantic_text("今天，去上海。"), semantic_text("今天去上海"))
        for left, right in (("-1.5", "15"), ("1.5", "15"),
                            ("2026-09-06", "2026/09/06"),
                            ("v1.2", "v12"), ("docs/v0.2", "docsv02")):
            self.assertNotEqual(semantic_text(left), semantic_text(right))

    def test_all_split_positions_and_single_char_fragments(self):
        value = '中文双流式，"引号"\\路径\n换行😀'
        for ascii_only in (False, True):
            raw = json.dumps({"text": value}, ensure_ascii=ascii_only)
            for split in range(len(raw) + 1):
                parser = SpeechArgumentStream()
                self.assertEqual(parser.feed(raw[:split]) + parser.feed(raw[split:]), value)
                self.assertEqual(parser.finish(), value)
            parser = SpeechArgumentStream()
            self.assertEqual("".join(parser.feed(c) for c in raw), value)
            self.assertEqual(parser.finish(), value)

    def test_rejects_duplicate_keys_extra_fields_bad_escapes_and_truncation(self):
        for raw in ('{"text":"a","text":"b"}', '{"text":"a","rate":2}',
                    '{"text":"\\x"}', '{"text":"\\uDC00"}', '{"text":"\\uD800x"}',
                    '{"text":1}', '{"other":"a"}', '{"text":"a"} trailing', '{"text":"', '{}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parser = SpeechArgumentStream()
                parser.feed(raw)
                parser.finish()

    def test_text_prefix_is_available_before_json_is_complete(self):
        parser = SpeechArgumentStream()
        self.assertEqual(parser.feed('{"text":"可以提前开始播放。'), '可以提前开始播放。')
        self.assertEqual(parser.feed('后续文字。"}'), '后续文字。')
        self.assertEqual(parser.finish(), '可以提前开始播放。后续文字。')

    def test_emotion_before_text_is_decoded_with_streamed_text(self):
        parser = SpeechArgumentStream()
        self.assertEqual(parser.feed('{"emotion":"happy","text":"开心'), '开心')
        self.assertEqual(parser.feed('地说。"}'), '地说。')
        self.assertEqual(parser.emotion, "happy")
        self.assertEqual(parser.finish(), "开心地说。")

    def test_invalid_or_late_nondefault_emotion_is_rejected(self):
        for raw in ('{"emotion":"whisper","text":"x"}', '{"text":"x","emotion":"happy"}'):
            parser = SpeechArgumentStream()
            with self.assertRaises(ValueError):
                parser.feed(raw)


class EpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = SpeculativeTools(FileTools((Path(self.temp.name),)))
        self.output = Output()
        self.events = []
        async def emit(kind, payload):
            self.events.append((kind, payload))
        self.emit = emit
        self.runtime = None

    def make(self, provider=None, **options):
        self.provider = provider or Provider()
        self.runtime = AgentEpisodeRuntime(self.provider, self.registry, self.output, self.emit,
            options=replace(V03Options(), balanced_interval_ms=1, silence_ms=30, **options))
        return self.runtime

    async def asyncTearDown(self):
        if self.runtime:
            await self.runtime.stop()
        self.registry.close()
        self.temp.cleanup()

    async def test_three_waiting_turns_keep_episode_and_do_not_poll(self):
        r = self.make()
        for index, text in enumerate(("你先不要回答", "我慢慢说", "这个问题分三个部分")):
            await r.observe("USER_INPUT_FINALIZED", turn_id=str(index), text=text)
            await idle(r)
        self.assertEqual(len(r.inputs), 3)
        self.assertEqual(len(self.provider.messages), 3)
        await asyncio.sleep(.08)
        self.assertEqual(len(self.provider.messages), 3)
        self.assertFalse(self.output.started)

    async def test_content_never_enters_speech(self):
        r = self.make(Provider([[{"type": "content", "text": "我会被显示但不能朗读"}]]))
        await r.speech_activity(False)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="说点什么")
        await idle(r)
        self.assertFalse(self.output.started)

    async def test_speaking_blocks_and_never_retries_same_request(self):
        r = self.make(Provider([[call("speak", {"text": "不该抢话"})]]))
        await r.speech_activity(True)
        await r.observe("USER_SEGMENT_UPDATED", turn_id="t", text="我还在讲话")
        await idle(r)
        self.assertEqual(r.request_count, 1)
        self.assertFalse(self.output.started)
        self.assertTrue(any(p.get("reason") == "user_speaking" for k, p in self.events if k == "agent.trace"))

    async def test_single_flight_uses_only_latest_dirty_context(self):
        provider = Provider([[call("speak", {"text": "旧结论"})]])
        provider.hold = asyncio.Event()
        r = self.make(provider)
        await r.speech_activity(False)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="第一版")
        await asyncio.sleep(.005)
        for text in ("第二版", "第三版", "最后版本"):
            await r.observe("USER_INPUT_FINALIZED", turn_id="t", text=text)
        provider.hold.set()
        await idle(r)
        self.assertEqual(provider.maximum, 1)
        self.assertEqual(len(provider.messages), 2)
        self.assertIn("最后版本", provider.messages[-1][1]["content"])
        self.assertFalse(self.output.started)

    async def test_silence_is_one_shot_and_cancelled_by_speech(self):
        r = self.make(decision_mode="conservative")
        await r.speech_activity(True)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="等我想一想")
        await idle(r)
        await r.speech_activity(False)
        await asyncio.sleep(.08)
        await idle(r)
        count = r.request_count
        await asyncio.sleep(.08)
        self.assertEqual(r.request_count, count)
        self.assertEqual(count, 2)
        await r.speech_activity(True)
        await r.speech_activity(False)
        await r.speech_activity(True)
        await asyncio.sleep(.06)
        self.assertEqual(r.request_count, count)

    async def test_finish_episode_starts_clean_context_on_next_input(self):
        r = self.make(Provider([[call("finish_episode", {})]]))
        await r.observe("USER_INPUT_FINALIZED", turn_id="a", text="任务完成")
        await idle(r)
        self.assertIsNone(r.episode_id)
        await r.observe("USER_INPUT_FINALIZED", turn_id="b", text="新任务")
        await idle(r)
        self.assertEqual(list(r.inputs), ["b"])
        self.assertNotIn("任务完成", self.provider.messages[-1][1]["content"])

    async def test_readonly_cache_reused_across_turns_and_refreshed(self):
        path = Path(self.temp.name) / "test.txt"
        path.write_text("first", encoding="utf-8")
        tool = call("files_read_text", {"path": str(path)})
        r = self.make(Provider([[tool], [call("wait_for_input", {})], [tool], [call("wait_for_input", {})],
                                [tool], [call("wait_for_input", {})]]))
        for turn in ("a", "b"):
            await r.observe("USER_INPUT_FINALIZED", turn_id=turn, text="读这个文件" + turn)
            await idle(r)
        events = [p for k, p in self.events if p.get("kind") == "TOOL_STARTED"]
        self.assertEqual([p["reused"] for p in events], [False, True])
        path.write_text("changed and longer", encoding="utf-8")
        await r.observe("USER_INPUT_FINALIZED", turn_id="c", text="再读一次")
        await idle(r)
        events = [p for k, p in self.events if p.get("kind") == "TOOL_STARTED"]
        self.assertFalse(events[-1]["reused"])

    async def test_completed_speculative_result_is_hidden_after_intent_change(self):
        path = Path(self.temp.name) / "speculative.txt"
        path.write_text("private result", encoding="utf-8")
        provider = Provider([[call("files_read_text", {"path": str(path)})],
                            [call("wait_for_input", {})]])
        r = self.make(provider)
        await r.speech_activity(True)
        await r.observe("USER_INPUT_FINALIZED", turn_id="a", text="先查这个文件")
        await idle(r)
        self.assertTrue(any(m["role"] == "tool" for m in r.protocol))
        await r.observe("USER_INPUT_FINALIZED", turn_id="b", text="取消查询，改聊旅行")
        await idle(r)
        latest = provider.messages[-1]
        self.assertFalse(any(m["role"] == "tool" and "private result" in m["content"] for m in latest))
        self.assertFalse(any(m["role"] == "tool" and "private result" in m["content"] for m in r.protocol))

    async def test_punctuation_refinement_does_not_wake_model(self):
        r = self.make()
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="今天去上海")
        await idle(r)
        count = r.request_count
        await r.observe("USER_INPUT_REFINED", turn_id="t", text="今天，去上海。")
        await idle(r)
        self.assertEqual(r.request_count, count)
        await r.observe("USER_INPUT_REFINED", turn_id="t", text="今天去北京")
        await idle(r)
        self.assertEqual(r.request_count, count + 1)

    async def test_conservative_ignores_provisional_but_honors_final(self):
        r = self.make(decision_mode="conservative")
        await r.observe("USER_SEGMENT_UPDATED", turn_id="t", text="还没说完")
        await r.observe("ENDPOINT_CANDIDATE", turn_id="t")
        await asyncio.sleep(.01)
        self.assertEqual(r.request_count, 0)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="说完了")
        await idle(r)
        self.assertEqual(r.request_count, 1)

    async def test_request_budget_exhaustion_is_silent(self):
        provider = Provider([[call("unknown", {})] for _ in range(7)])
        r = self.make(provider)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="继续查询")
        await idle(r)
        self.assertEqual(r.request_count, 5)
        self.assertFalse(self.output.started)
        self.assertTrue(any(p.get("reason") == "request_budget" for k, p in self.events))

    async def test_context_budget_compacts_but_keeps_running(self):
        r = self.make(max_context_tokens=20)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="等我叫你再回答")
        await idle(r)
        self.assertEqual(r.request_count, 1)
        self.assertEqual(r.inputs["t"]["text"], "等我叫你再回答")
        self.assertTrue(r.context_compacted)
        await r.observe("USER_INPUT_FINALIZED", turn_id="later", text="新输入仍会进入 Agent 上下文")
        await idle(r)
        self.assertIn("later", r.inputs)
        self.assertEqual(r.request_count, 2)

    async def test_provider_prompt_usage_is_observed_separately_from_estimate(self):
        r = self.make(Provider([[{"type": "usage", "usage": {"prompt_tokens": 321}},
                                 call("wait_for_input", {})]]))
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="记录 usage")
        await idle(r)
        self.assertEqual(r.provider_prompt_tokens, 321)
        self.assertGreater(r.context_token_estimate, 0)
        self.assertTrue(any(p.get("kind") == "LLM_USAGE" and p.get("prompt_tokens") == 321
                            for _kind, p in self.events))

    async def test_finish_cancels_inflight_request(self):
        provider = Provider()
        provider.hold = asyncio.Event()
        r = self.make(provider)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="等待")
        await asyncio.sleep(.01)
        self.assertEqual(provider.active, 1)
        await r.end("fatal_error")
        await idle(r)
        self.assertEqual(provider.active, 0)
        self.assertIsNone(r.episode_id)

    async def test_only_one_speak_and_no_parallel_business_action(self):
        r = self.make(Provider([[call("speak", {"text": "第一句话。"}),
                                 call("speak", {"text": "第二句话。"}, 1),
                                 call("files_search", {"query": "anything"}, 2)]]))
        await r.speech_activity(False)
        await r.observe("USER_INPUT_FINALIZED", turn_id="t", text="请回答")
        await idle(r)
        self.assertEqual("".join(self.output.texts), "第一句话。")
        self.assertEqual(len(self.output.started), 1)
        self.assertFalse(self.registry.entries)
        results = [json.loads(m["content"]) for m in r.protocol if m["role"] == "tool"]
        self.assertEqual([v["status"] for v in results], ["accepted", "blocked", "blocked"])

    async def test_late_tool_result_not_added_to_new_context(self):
        r = self.make(Provider([[call("files_search", {"query": "a"})]]))
        started, release = asyncio.Event(), asyncio.Event()
        original = self.registry._execute
        async def delayed(*args):
            started.set()
            await release.wait()
            return await original(*args)
        self.registry._execute = delayed
        await r.observe("USER_INPUT_FINALIZED", turn_id="a", text="查文件")
        await asyncio.wait_for(started.wait(), 1)
        await r.observe("USER_INPUT_FINALIZED", turn_id="b", text="不用查文件了")
        release.set()
        await idle(r)
        self.assertFalse(any(m["role"] == "tool" and '"files"' in m["content"] for m in r.protocol))
        self.assertTrue(any(p.get("kind") == "TOOL_RESULT_DISCARDED" for k, p in self.events))

    async def test_readonly_tools_run_in_parallel(self):
        provider = Provider([[call("files_search", {"query": "a"}),
                              call("files_search", {"query": "b"}, 1)]])
        r = self.make(provider)
        active = maximum = 0
        both_started = asyncio.Event()
        original = r.registry._execute

        async def delayed(backend, name, arguments, mark_started):
            nonlocal active, maximum
            mark_started(time.monotonic())
            active += 1
            maximum = max(maximum, active)
            if active >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            await asyncio.sleep(.03)
            active -= 1
            return await original(backend, name, arguments, lambda _when: None)

        r.registry._execute = delayed
        await r.observe("USER_INPUT_FINALIZED", turn_id="parallel", text="查两个文件")
        await asyncio.wait_for(both_started.wait(), 1)
        await idle(r)
        self.assertEqual(maximum, 2)

    async def test_web_search_prefetch_does_not_hold_agent_step(self):
        class Search:
            available = True
            service_status = "READY"

            def schemas(self):
                return [{"type": "function", "function": {"name": "web_search",
                         "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                         "required": ["query"], "additionalProperties": False}}}]

            def normalize(self, name, arguments):
                return name, {"query": arguments["query"].strip()}

            def execute(self, name, arguments):
                return {"organic": [], "related_searches": []}

            def fresh(self, name, result):
                return True

        self.registry.search = Search()
        provider = Provider([[call("web_search", {"query": "最新消息"})],
                            [call("wait_for_input", {})]])
        r = self.make(provider)
        started, release = asyncio.Event(), asyncio.Event()
        original = r.registry._execute

        async def delayed(*args):
            started.set()
            await release.wait()
            return await original(*args)

        r.registry._execute = delayed
        await r.speech_activity(True)
        await r.observe("USER_INPUT_FINALIZED", turn_id="search", text="先查一下，等我说完")
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(.01)
        self.assertIsNone(r.active_request)
        self.assertEqual(r.request_count, 1)
        self.assertTrue(any(p.get("kind") == "TOOL_PREFETCHED" for k, p in self.events))
        release.set()
        for _ in range(100):
            if not r._background_tools:
                break
            await asyncio.sleep(.002)
        self.assertFalse(r._background_tools)

    async def test_web_search_prefetch_releases_silent_step_and_continues_with_result(self):
        class Search:
            available = True
            service_status = "READY"

            def schemas(self):
                return [{"type": "function", "function": {"name": "web_search",
                         "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                         "required": ["query"], "additionalProperties": False}}}]

            def normalize(self, name, arguments):
                return name, {"query": arguments["query"].strip()}

            def execute(self, name, arguments):
                return {"organic": [{"title": "cached result", "link": "https://example.test",
                                      "snippet": "result is available"}], "related_searches": []}

            def fresh(self, name, result):
                return True

        self.registry.search = Search()
        provider = Provider([[call("web_search", {"query": "最新消息"}),
                              call("web_search", {"query": "天气"}, 1)],
                            [call("speak", {"text": "搜索完成。"})]])
        r = self.make(provider)
        started, release = asyncio.Event(), asyncio.Event()
        original = r.registry._execute

        async def delayed(*args):
            started.set()
            await release.wait()
            return await original(*args)

        r.registry._execute = delayed
        await r.speech_activity(False)
        await r.observe("USER_INPUT_FINALIZED", turn_id="search", text="搜索并回答")
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(100):
            if r.active_request is None:
                break
            await asyncio.sleep(.002)
        self.assertIsNone(r.active_request)
        self.assertEqual(r.request_count, 1)
        release.set()
        for _ in range(200):
            if len(provider.messages) >= 2 and not r._background_tools:
                break
            await asyncio.sleep(.002)
        await idle(r)
        self.assertGreaterEqual(len(provider.messages), 2)
        tool_messages = [m for m in provider.messages[-1] if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertTrue(all("cached result" in m["content"] for m in tool_messages))
        self.assertEqual("搜索完成。", "".join(r.output.texts))

    async def test_large_tool_result_is_bounded_in_model_context(self):
        r = self.make()
        raw = {"organic": [{"title": "t" * 256, "link": "https://example.test/" + "a" * 100,
                             "snippet": "s" * 2000, "date": "2026"} for _ in range(10)],
               "related_searches": [{"query": "q" * 256} for _ in range(8)]}
        r.episode_id, r.agent_step_id, r.context_version = "ep", "step", 1
        r.protocol = [{"role": "assistant", "content": None, "tool_calls": [call("web_search", {"query": "x"})["call"]]},
                      {"role": "tool", "tool_call_id": "call_0", "content": json.dumps(raw, ensure_ascii=False)}]
        messages = r.messages(set())
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertLess(len(serialized), 20000)
        self.assertIn("context_truncated", serialized)
        tool = next(item for item in messages if item["role"] == "tool")
        compacted = json.loads(tool["content"])
        self.assertLessEqual(len(tool["content"]), r.CONTEXT_TOOL_RESULT_CHARS)
        self.assertLessEqual(len(compacted["organic"]), 2)
        self.assertNotIn("related_searches", compacted)


def make_controller(provider):
    player = PcmPlayer(sample_rate=32000, channels=1, output_device=None)
    c = EpisodeController(agent_options=replace(V03Options(), balanced_interval_ms=1),
        options=replace(V02Options(), enabled=True), analysis=FakeAnalysis(),
        state=RuntimeState(), bus=EventBus(), history=InMemoryHistory(), capture=None, kws=None,
        vad=FakeVad(), asr=FakeAsr(), llm=provider, tts=PacingTts(player), player=player,
        wake_enabled=False, wake_keyword="hei claw", wake_preroll_ms=1200,
        vad_min_speech_ms=250, vad_end_silence_ms=700, max_turn_ms=15000,
        bridge_min_chars=8, bridge_max_chars=36, bridge_max_wait_ms=350, bridge_breaks="。！？；，、")
    player.on_playback_started = c._on_playback_started
    c.state.listening = True
    return c


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_pcm_is_retried_once_on_a_fresh_tts_session(self):
        c = make_controller(Provider())

        class EmptyThenAudioSession:
            def __init__(self, produce_audio):
                self.produce_audio = produce_audio
                self.audio = asyncio.Queue()

            async def start(self):
                pass

            async def send_text(self, _text):
                if self.produce_audio:
                    await self.audio.put(b"\x01\x00" * 4096)

            async def finish(self):
                await self.audio.put(None)

            async def audio_chunks(self):
                while True:
                    value = await self.audio.get()
                    if value is None:
                        return
                    yield value

            async def close(self):
                pass

        sessions = []

        class FlakyTts:
            transport = "websocket"

            def session(self):
                session = EmptyThenAudioSession(produce_audio=bool(sessions))
                sessions.append(session)
                return session

        c.output.tts = FlakyTts()
        draining = True

        async def drain_player():
            while draining:
                c.player._callback(bytearray(1280), 640, None, None)
                await asyncio.sleep(.002)

        drain = asyncio.create_task(drain_player())
        try:
            r = c.episode
            r.episode_id, r.agent_step_id, r.context_version, r.user_speaking = "ep", "step", 1, False
            output = await c.output.begin(r.identity(), "t")
            await output.feed("这一次应该在空 PCM 后重试并播放。")
            await output.finish()
            await asyncio.wait_for(output.task, 2)
            self.assertEqual(len(sessions), 2)
            self.assertEqual(r.outputs[-1]["status"], "completed")
            self.assertNotEqual(r.outputs[-1]["delivery"]["status"], "none")
            self.assertTrue(any(item["kind"] == "TTS_EMPTY_AUDIO" for item in r.trace))
        finally:
            draining = False
            await drain
            await c.close()

    async def test_bidi_next_output_waits_for_cancel_barrier(self):
        cancel_started, release = asyncio.Event(), asyncio.Event()
        c = make_controller(Provider())
        class Session(PacingTtsSession):
            bidi = True
            reusable = True
            generation_finished = False
            starts = 0
            async def start(self):
                self.starts += 1
                self.generation_finished = False
            async def cancel(self, **kwargs):
                cancel_started.set()
                await release.wait()
                while not self.audio.empty():
                    self.audio.get_nowait()
                self.generation_finished = True
                return True
            async def flush(self):
                self.generation_finished = True
                await self.finish()
        session = Session(c.player)
        c.output.tts = type("Tts", (), {"transport": "websocket_bidi", "session": lambda _: session})()
        next_output = None
        try:
            r = c.episode
            r.episode_id, r.agent_step_id, r.context_version, r.user_speaking = "ep", "step", 1, False
            first = await c.output.begin(r.identity(), "t")
            await first.feed("第一段语音将被取消。")
            for _ in range(200):
                if session.sent:
                    break
                await asyncio.sleep(.005)
            self.assertTrue(session.sent)
            c.output.invalidate("stale")
            await asyncio.wait_for(cancel_started.wait(), 1)
            r.context_version += 1
            next_output = asyncio.create_task(c.output.begin(r.identity(), "t"))
            await asyncio.sleep(.01)
            self.assertFalse(next_output.done())
            self.assertEqual(session.starts, 1)
            release.set()
            second = await asyncio.wait_for(next_output, 1)
            await second.feed("第二段不能混入旧音频。")
            for _ in range(200):
                if session.starts == 2:
                    break
                await asyncio.sleep(.005)
            self.assertEqual(session.starts, 2)
            self.assertNotEqual(first.workspace.playback_key, second.workspace.playback_key)
        finally:
            release.set()
            if next_output and not next_output.done():
                next_output.cancel()
                await asyncio.gather(next_output, return_exceptions=True)
            await c.close()

    async def test_invalid_suffix_stops_buffered_pcm_without_fallback(self):
        suffix = asyncio.Event()
        class Broken:
            async def stream_events(self, messages, **kwargs):
                yield {"type": "tool_delta", "call": {"index": 0, "id": "s", "function": {
                    "name": "speak", "arguments": '{"text":"这一句话足够长，可以开始合成。'}}}
                await suffix.wait()
                yield {"type": "tool_delta", "call": {"index": 0, "function": {"arguments": '","extra":true}'}}}
                yield {"type": "finish", "reason": "tool_calls"}
        c = make_controller(Broken())
        try:
            await c.episode.speech_activity(False)
            await c.episode.observe("USER_INPUT_FINALIZED", turn_id="t", text="回答")
            for _ in range(200):
                if c.player.buffered_ms:
                    break
                await asyncio.sleep(.005)
            self.assertGreater(c.player.buffered_ms, 0)
            suffix.set()
            await idle(c.episode)
            await c.output.close()
            self.assertEqual(c.player.buffered_ms, 0)
            self.assertEqual(c.episode.outputs[-1]["status"], "failed")
            self.assertEqual(c.episode.outputs[-1]["delivery"]["heard_text"], "")
        finally:
            suffix.set()
            await c.close()

    async def test_cancel_before_first_delta_is_recorded(self):
        c = make_controller(Provider())
        try:
            await c.episode.speech_activity(False)
            await c.episode.observe("USER_INPUT_FINALIZED", turn_id="t", text="等待")
            await idle(c.episode)
            await c.output.begin(c.episode.identity(), "t")
            c.output.invalidate("stale")
            await c.output.close()
            self.assertEqual(c.episode.outputs[-1]["status"], "stale")
            self.assertEqual(c.player.buffered_ms, 0)
        finally:
            await c.close()

    async def test_double_stream_starts_pcm_before_tool_arguments_finish(self):
        release = asyncio.Event()
        class Streaming:
            async def stream_events(self, messages, **kwargs):
                yield {"type": "tool_delta", "call": {"index": 0, "id": "s",
                       "function": {"name": "speak", "arguments": '{"text":"第一句话在模型结束以前就开始播放。'}}}
                await release.wait()
                yield {"type": "tool_delta", "call": {"index": 0, "function": {"arguments": '后半句继续。"}'}}}
                yield {"type": "finish", "reason": "tool_calls"}
        c = make_controller(Streaming())
        try:
            await c.episode.speech_activity(False)
            await c.episode.observe("USER_INPUT_FINALIZED", turn_id="t", text="请解释一下")
            for _ in range(300):
                if c.player.buffered_ms:
                    break
                await asyncio.sleep(.005)
            self.assertGreater(c.player.buffered_ms, 0)
            self.assertFalse(release.is_set())
            c.player._callback(bytearray(1280), 640, None, None)
            self.assertTrue(c.output.handle.workspace.playback_started)
            await c.episode.speech_activity(True)
            self.assertEqual(c.player.buffered_ms, 0)
            release.set()
            await idle(c.episode)
            await c.output.close()
            self.assertEqual(c.episode.outputs[-1]["status"], "interrupted")
            self.assertNotEqual(c.episode.outputs[-1]["delivery"]["status"], "full")
        finally:
            release.set()
            await c.close()

    async def test_committed_input_history_is_independent_of_speech(self):
        c = make_controller(Provider())
        try:
            for i in range(3):
                frame = AudioFrame(b"\x00\x00" * 16000, i * 16000, time.monotonic())
                c._recent.clear()
                c._recent.append(frame)
                await c._new_turn(frame)
                ws = c.workspace
                ws.final_text = f"第{i}段，先别回答"
                ws.committed = True
                await c._input_committed(ws, ws.token)
                await idle(c.episode)
                self.assertIsNone(c.workspace)
            self.assertEqual(len(c.history.records), 3)
            self.assertEqual(len({r.episode_id for r in c.history.records}), 1)
            self.assertEqual(len(c.episode.inputs), 3)
            self.assertIsNone(c.scheduler)
        finally:
            await c.close()

    async def test_stop_mid_input_keeps_episode_identity_and_writes_once(self):
        c = make_controller(Provider())
        try:
            frame = AudioFrame(b"\x00\x00" * 16000, 0, time.monotonic())
            c._recent.append(frame)
            await c._new_turn(frame)
            ws = c.workspace
            ws.raw_partial = "用户还没有说完"
            await c.stop_listening()
            records = [record for record in c.history.records if record.turn_id == ws.turn_id]
            self.assertEqual(len(records), 1)
            self.assertIsNotNone(records[0].episode_id)
            self.assertEqual(records[0].episode_id, c.episode_history[ws.turn_id])
            await c._cancel_turn(reason="stopped")
            self.assertEqual(len([record for record in c.history.records if record.turn_id == ws.turn_id]), 1)
        finally:
            await c.close()

    async def test_reset_keeps_completed_history_but_starts_a_clean_episode(self):
        c = make_controller(Provider())
        try:
            await c.episode.observe("USER_INPUT_FINALIZED", turn_id="old", text="旧任务")
            await idle(c.episode)
            old_episode = c.episode.episode_id
            self.assertIsNotNone(old_episode)
            await c.reset_session()
            self.assertIsNone(c.episode.episode_id)
            self.assertTrue(c.state.listening)
            await c.episode.observe("USER_INPUT_FINALIZED", turn_id="new", text="新任务")
            await idle(c.episode)
            self.assertNotEqual(c.episode.episode_id, old_episode)
            self.assertEqual(list(c.episode.inputs), ["new"])
        finally:
            await c.close()

    async def test_old_output_completion_trace_keeps_old_identity(self):
        c = make_controller(Provider())
        events = []
        async def emit(kind, payload):
            events.append((kind, payload))
        c.episode.emit = emit
        try:
            r = c.episode
            r.episode_id, r.agent_step_id, r.context_version = "old-episode", "old-step", 1
            old = r.identity()
            await r.end("finished")
            await r.observe("USER_INPUT_FINALIZED", turn_id="new-turn", text="新任务")
            await idle(r)
            current = r.identity()
            current_snapshot = dict(c.state.agent)
            await r.output_completed({"speech_id": "old-speech", "episode_id": old[0],
                                      "agent_step_id": old[1], "context_version": old[2],
                                      "turn_id": "old-turn", "generation_id": 1,
                                      "text": "旧回答", "status": "interrupted", "delivery": {}})
            traces = [payload for kind, payload in events
                      if kind == "agent.trace" and payload.get("kind") == "TTS_INTERRUPTED"]
            self.assertTrue(traces)
            self.assertEqual(traces[-1]["episode_id"], old[0])
            self.assertEqual(r.identity(), current)
            self.assertEqual(c.state.agent, current_snapshot)
        finally:
            await c.close()
