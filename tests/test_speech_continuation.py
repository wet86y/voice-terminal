"""连续语音交接：使用真实播放器队列，控制音频消费与历史结算边界。"""

import asyncio
import time
from types import SimpleNamespace

from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.streamed_speech import StreamedSpeechAdmissionState, queue_streamed_speech
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


READY_TIMEOUT_SECONDS = 2.0  # READY_TIMEOUT_SECONDS（测试准备阶段超时秒数）。
HANDOFF_TIMEOUT_SECONDS = 0.5  # HANDOFF_TIMEOUT_SECONDS（播放交接最大允许秒数）。
FIRST_TEXT = "第一句。"
SUFFIX_TEXT = "第二句。"


class ContinuationHarness:
    """隔离外部语音服务，保留完整输出流程及 PCM（原始音频采样）队列。"""

    def __init__(self, *, device_latency_ms=0):
        self.identity = ("conversation", "request")
        self.turn_id = "turn"
        self.events = []
        self.records = []
        self.background = []
        self.settlement_gate = None
        self.settlement_started = asyncio.Event()

        harness = self

        class Session:
            bidi = True
            completed_segments = 0

            def __init__(self, index):
                self.index = index
                self.received_bytes = 0
                self.sent = []
                self.audio = asyncio.Queue()
                self.closed = False
                self.generation_finished = False

            @property
            def reusable(self):
                return not self.closed and not self.generation_finished

            async def start(self):
                harness.events.append(("provider_started", self.index))

            async def send_text(self, text):
                self.sent.append(text)
                pcm = bytes((self.index, 0)) * 80
                self.received_bytes += len(pcm)
                await self.audio.put(pcm)

            async def audio_chunks(self):
                while True:
                    pcm = await self.audio.get()
                    if pcm is None:
                        return
                    yield pcm

            async def finish(self):
                self.generation_finished = True
                await self.audio.put(None)

            async def flush(self):
                await self.finish()

            async def cancel(self, **_kwargs):
                await self.audio.put(None)

            async def close(self):
                self.closed = True

        class Tts:
            transport = "websocket_bidi"
            effective_scheduling = "direct"

            def __init__(self):
                self.sessions = []

            def session(self, **_kwargs):
                session = Session(len(self.sessions) + 1)
                self.sessions.append(session)
                return session

        class Player(PcmPlayer):
            def __init__(self):
                super().__init__(
                    sample_rate=1000, channels=1, output_device=None,
                    device_latency_ms=device_latency_ms,
                )
                self.drain_started = asyncio.Queue()

            def allow(self, key):
                harness.events.append(("player_allowed", key))
                super().allow(key)

            async def wait_until_output_complete(self, timeout_seconds=10):
                await self.drain_started.put(self._active_turn)
                await super().wait_until_output_complete(timeout_seconds)

        async def emit(kind, payload):
            harness.events.append((kind, payload))

        async def log(kind, **payload):
            harness.events.append((kind, payload))

        async def output_completed(record):
            harness.records.append(record)
            harness.events.append(("history_started", record["speech_id"]))
            harness.settlement_started.set()
            if harness.settlement_gate is not None and len(harness.records) == 1:
                await harness.settlement_gate.wait()
            harness.events.append(("history_finished", record["speech_id"]))

        async def output_transaction_settled(record):
            harness.events.append(("transaction_settled", record["speech_id"]))

        def track(task):
            harness.background.append(task)
            return task

        self.tts = Tts()
        self.player = Player()
        self.runtime = SimpleNamespace(
            gate=lambda _identity: None, emit=emit, log=log,
            output_completed=output_completed,
            output_transaction_settled=output_transaction_settled,
            user_speaking=False, closed=False, request_started_at=None,
            input_finished_at={},
        )
        controller = SimpleNamespace(
            tts=self.tts, player=self.player,
            options=SimpleNamespace(tts_pacing=False, tts_lead_ms=1, tts_local_buffer_ms=1000),
            bridge_options={}, _turn_timings={}, _track=track,
            state=SimpleNamespace(current={}, latency={}),
            _add_playback_delivery_fields=lambda _delivery, _snapshot: None,
        )
        self.output = EpisodeSpeechOutput(controller)
        self.output.runtime = self.runtime
        self.player.on_playback_started = self.output.playback_started

    async def first(self):
        handle = await self.output.begin(self.identity, self.turn_id)
        await handle.feed(FIRST_TEXT)
        await handle.finish()
        await self.wait_for_drain(handle)
        return handle

    async def wait_for_drain(self, handle=None, timeout=READY_TIMEOUT_SECONDS):
        key = await asyncio.wait_for(self.player.drain_started.get(), timeout)
        handle = handle or self.output.handle
        assert key == handle.workspace.playback_key
        return handle

    def render(self, frames=80):
        pcm = bytearray(frames * 2)
        self.player._callback(pcm, frames, None, None)
        return bytes(pcm)

    async def close(self):
        if self.settlement_gate is not None:
            self.settlement_gate.set()
        await self.output.close()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)


def _agent(harness):
    conversation = ConversationRuntime()
    conversation.begin_turn(harness.turn_id)
    harness.identity = (conversation.conversation_id, "request")
    registry = SimpleNamespace(begin_turn=lambda _turn: None, action_definitions=lambda: [])
    runtime = AgentEpisodeRuntime(None, registry, harness.output, harness.runtime.emit,
                                  conversation=conversation, ledger=SessionLedger())
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = harness.turn_id
    return runtime


def test_new_speak_is_rejected_until_actual_pcm_playback_completes():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            decision = DecisionEnvelope(SpeechDecision("speak", "calm", SUFFIX_TEXT), ())
            await runtime._apply_decision_v06(harness.identity, harness.turn_id, decision)
            assert await harness.output.begin(harness.identity, harness.turn_id) is None
            assert harness.output.handle is first
            assert len(harness.tts.sessions) == 1
            assert harness.render() == b"\x01\x00" * 80
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert not harness.output.busy
            # 播放完成后的一秒仍使用 revision（尾巴修订），只提交新增正文。
            decision = DecisionEnvelope(SpeechDecision("revision", "calm", SUFFIX_TEXT), ())
            await runtime._apply_decision_v06(harness.identity, harness.turn_id, decision)
            second = await harness.wait_for_drain()
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            harness.render()
            await asyncio.wait_for(asyncio.shield(second.task), HANDOFF_TIMEOUT_SECONDS)
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_empty_local_audio_queue_does_not_open_device_output_boundary():
    async def scenario():
        harness = ContinuationHarness(device_latency_ms=40)
        try:
            first = await harness.first()
            harness.render()
            progress = harness.player.progress(first.workspace.playback_key)
            assert progress["buffered_ms"] == 0
            assert progress["output_estimated_ms"] < progress["enqueued_ms"]
            assert await harness.output.begin(harness.identity, harness.turn_id) is None
            assert len(harness.tts.sessions) == 1
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert first.snapshot.output_estimated_ms == first.snapshot.enqueued_ms
            assert not harness.output.busy
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_settlement_stays_busy_and_rejected_request_is_never_replayed():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            harness.settlement_gate = asyncio.Event()
            harness.render()
            await asyncio.wait_for(harness.settlement_started.wait(), HANDOFF_TIMEOUT_SECONDS)
            assert harness.output.busy
            assert harness.output.speak_snapshot["queue_status"] == "continuation"
            state = StreamedSpeechAdmissionState(
                harness.identity, harness.turn_id, dict(harness.output.speak_snapshot),
                0, None, False, None)
            decision = SpeechDecision("speak", "calm", SUFFIX_TEXT)
            await queue_streamed_speech(runtime, state, decision, SUFFIX_TEXT[:1])
            assert state.rejected
            assert await harness.output.begin(harness.identity, harness.turn_id) is None
            harness.settlement_gate.set()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            await queue_streamed_speech(runtime, state, decision, SUFFIX_TEXT, final=True)
            assert state.speech_handle is None
            assert len(harness.tts.sessions) == 1
            # 新请求可以空闲创建，但仍遵守用户讲话的播放保持。
            harness.runtime.user_speaking = True
            fresh = await harness.output.begin(harness.identity, harness.turn_id)
            await fresh.feed(SUFFIX_TEXT)
            await fresh.finish()
            assert not fresh.playback_ready.is_set()
            assert await harness.output.begin(harness.identity, harness.turn_id) is None
            harness.runtime.user_speaking = False
            harness.output.release_held()
            await harness.wait_for_drain(fresh)
            harness.render()
            await asyncio.wait_for(asyncio.shield(fresh.task), HANDOFF_TIMEOUT_SECONDS)
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_cancel_with_text_replaces_busy_output_and_rejects_old_audio():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            assert harness.render(20) == b"\x01\x00" * 20
            decision = DecisionEnvelope(SpeechDecision("cancel", "calm", "我刚才听错了，改口。"), ())
            await runtime._apply_decision_v06(harness.identity, harness.turn_id, decision)
            replacement = harness.output.handle
            await harness.wait_for_drain(replacement)
            harness.player.enqueue(first.workspace.playback_key, b"\x09\x00" * 80)
            assert harness.render() == b"\x02\x00" * 80
            await asyncio.wait_for(asyncio.shield(replacement.task), HANDOFF_TIMEOUT_SECONDS)
            assert first.reason == "replaced"
            assert harness.records[-1]["text"] == "我刚才听错了，改口。"
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_late_same_turn_decision_uses_settled_prefix_without_replaying_it():
    async def scenario():
        harness = ContinuationHarness()
        try:
            conversation = ConversationRuntime()
            conversation.begin_turn(harness.turn_id)
            harness.identity = (conversation.conversation_id, "request")
            registry = SimpleNamespace(begin_turn=lambda _turn: None, action_definitions=lambda: [])
            runtime = AgentEpisodeRuntime(
                None, registry, harness.output, harness.runtime.emit,
                conversation=conversation, ledger=SessionLedger(),
            )
            runtime.episode_id = conversation.conversation_id
            runtime.last_turn_id = harness.turn_id

            first = await harness.first()
            harness.render()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            # 窗口结束后才恢复普通新播报；缩短截止时间避免测试真实等待一秒。
            first.continuation_deadline = time.monotonic() - 1
            first.continuation_signal.set()
            assert harness.output.speak_snapshot["queue_status"] == "empty"
            assert harness.output.speak_snapshot["delivered_text"] == FIRST_TEXT

            decision = DecisionEnvelope(SpeechDecision("speak", "calm", FIRST_TEXT + SUFFIX_TEXT), ())
            await runtime._apply_decision_v06(harness.identity, harness.turn_id, decision)
            successor = harness.output.handle
            await harness.wait_for_drain(successor, HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            harness.render()
            await asyncio.wait_for(asyncio.shield(successor.task), HANDOFF_TIMEOUT_SECONDS)
            assert harness.output.delivered["text"] == FIRST_TEXT + SUFFIX_TEXT
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_request_created_while_busy_cannot_start_after_playback_finishes():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            state = StreamedSpeechAdmissionState(
                harness.identity, harness.turn_id, dict(harness.output.speak_snapshot),
                len(FIRST_TEXT), None, True, None)
            # 模型尚未返回文本，旧播报已播放并结算；仍按请求时的忙碌边界拒绝。
            harness.render()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            decision = SpeechDecision("speak", "calm", FIRST_TEXT + SUFFIX_TEXT)
            await queue_streamed_speech(runtime, state, decision, decision.text, final=True)
            assert state.rejected and state.speech_handle is None
            assert len(harness.tts.sessions) == 1
        finally:
            await harness.close()
    asyncio.run(scenario())


def test_concurrent_begin_confirms_idle_after_async_preparation():
    async def scenario():
        harness = ContinuationHarness()
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()
        async def prepare():
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
        harness.output.controller._on_model_speech_accepted = prepare
        try:
            tasks = [asyncio.create_task(harness.output.begin(harness.identity, harness.turn_id))
                     for _ in range(2)]
            await asyncio.wait_for(both_entered.wait(), READY_TIMEOUT_SECONDS)
            release.set()
            handles = await asyncio.gather(*tasks)
            assert sum(handle is not None for handle in handles) == 1
            accepted = next(handle for handle in handles if handle is not None)
            await accepted.feed(FIRST_TEXT)
            await accepted.finish()
            await harness.wait_for_drain(accepted)
            harness.render()
            await asyncio.wait_for(asyncio.shield(accepted.task), HANDOFF_TIMEOUT_SECONDS)
            assert len(harness.tts.sessions) == 1
        finally:
            await harness.close()
    asyncio.run(scenario())
