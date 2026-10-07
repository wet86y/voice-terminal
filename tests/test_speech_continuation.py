"""连续语音交接：使用真实播放器队列，控制音频消费与历史结算边界。"""

import asyncio
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

    async def append(self, first):
        handle = await self.output.append_successor(
            self.identity, self.turn_id,
            baseline_speech_id=first.speech_id,
            baseline_generation=first.workspace.generation,
            suffix=SUFFIX_TEXT,
        )
        await handle.finish()
        return handle

    async def wait_for_drain(self, handle, timeout=READY_TIMEOUT_SECONDS):
        key = await asyncio.wait_for(self.player.drain_started.get(), timeout)
        assert key == handle.workspace.playback_key

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


def test_queued_suffix_starts_promptly_after_previous_pcm_drains():
    async def scenario():
        harness = ContinuationHarness()
        try:
            first = await harness.first()
            successor = await harness.append(first)
            assert len(harness.tts.sessions) == 1
            assert harness.player.buffered_ms == 80
            assert harness.render() == b"\x01\x00" * 80

            await harness.wait_for_drain(successor, HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[0].sent == [FIRST_TEXT]
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            assert first.snapshot.enqueued_ms == first.snapshot.output_estimated_ms == 80
            assert harness.records[0]["delivery"]["heard_text"] == FIRST_TEXT
            assert harness.output.speak_snapshot["text"] == FIRST_TEXT + SUFFIX_TEXT

            settled = harness.events.index(("transaction_settled", first.speech_id))
            allowed = harness.events.index(("player_allowed", successor.workspace.playback_key))
            assert settled < allowed
            assert harness.render() == b"\x02\x00" * 80
            await asyncio.wait_for(asyncio.shield(successor.task), HANDOFF_TIMEOUT_SECONDS)
            assert harness.output.delivered["text"] == FIRST_TEXT + SUFFIX_TEXT
            assert [record["status"] for record in harness.records] == ["completed", "completed"]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_suffix_still_waits_for_device_output_after_local_queue_is_empty():
    async def scenario():
        harness = ContinuationHarness(device_latency_ms=40)
        try:
            first = await harness.first()
            successor = await harness.append(first)
            harness.render()
            progress = harness.player.progress(first.workspace.playback_key)
            assert progress["buffered_ms"] == 0
            assert progress["output_estimated_ms"] < progress["enqueued_ms"]
            assert first.snapshot is None
            assert len(harness.tts.sessions) == 1

            await harness.wait_for_drain(successor, HANDOFF_TIMEOUT_SECONDS)
            assert first.snapshot.output_estimated_ms == first.snapshot.enqueued_ms
            harness.render()
            await asyncio.wait_for(asyncio.shield(successor.task), HANDOFF_TIMEOUT_SECONDS)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_suffix_preserves_settlement_boundary_and_user_speech_hold():
    async def scenario():
        harness = ContinuationHarness()
        try:
            first = await harness.first()
            successor = await harness.append(first)
            harness.settlement_gate = asyncio.Event()
            harness.render()
            await asyncio.wait_for(harness.settlement_started.wait(), HANDOFF_TIMEOUT_SECONDS)
            assert harness.output.busy
            assert first.snapshot.buffered_ms == 0
            assert len(harness.tts.sessions) == 1

            harness.runtime.user_speaking = True
            assert harness.output.hold_unplayed() == successor.speech_id
            harness.settlement_gate.set()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert len(harness.tts.sessions) == 1
            assert not successor.playback_ready.is_set()

            harness.runtime.user_speaking = False
            assert harness.output.release_held() == successor.speech_id
            await harness.wait_for_drain(successor, HANDOFF_TIMEOUT_SECONDS)
            harness.render()
            await asyncio.wait_for(asyncio.shield(successor.task), HANDOFF_TIMEOUT_SECONDS)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_barge_in_cancels_queued_suffix_and_rejects_retired_audio():
    async def scenario():
        harness = ContinuationHarness()
        try:
            first = await harness.first()
            successor = await harness.append(first)
            assert harness.render(20) == b"\x01\x00" * 20
            harness.output.cancel_all("interrupted")
            await asyncio.wait_for(
                asyncio.gather(first.task, successor.task, return_exceptions=True),
                READY_TIMEOUT_SECONDS,
            )
            assert first.reason == successor.reason == "interrupted"
            assert len(harness.tts.sessions) == 1

            replacement = await harness.output.replace_current(harness.identity, "new-turn")
            await replacement.feed("改口后的句子。")
            await replacement.finish()
            await harness.wait_for_drain(replacement)
            harness.player.enqueue(first.workspace.playback_key, b"\x09\x00" * 80)
            assert harness.render() == b"\x02\x00" * 80
            await asyncio.wait_for(asyncio.shield(replacement.task), HANDOFF_TIMEOUT_SECONDS)
            assert harness.records[-1]["text"] == "改口后的句子。"
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


def test_cached_active_prefix_survives_completion_during_model_stream():
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
            harness.render(20)
            state = StreamedSpeechAdmissionState(
                identity=harness.identity, source_turn_id=harness.turn_id,
                snapshot=harness.output.speak_snapshot,
                context_locked_chars=len(FIRST_TEXT), system_check_id=None,
                same_turn_transcript_reassessment=True, dispatch_context=None,
            )
            decision = SpeechDecision("speak", "calm", FIRST_TEXT + SUFFIX_TEXT)
            assert not await queue_streamed_speech(runtime, state, decision, FIRST_TEXT[:1])
            assert state.prefix_baseline["source"] == "active_same_turn"

            harness.render(60)
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert await queue_streamed_speech(runtime, state, decision, FIRST_TEXT + SUFFIX_TEXT)
            successor = state.speech_handle
            await successor.finish()
            await harness.wait_for_drain(successor, HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            harness.render()
            await asyncio.wait_for(asyncio.shield(successor.task), HANDOFF_TIMEOUT_SECONDS)
        finally:
            await harness.close()

    asyncio.run(scenario())
