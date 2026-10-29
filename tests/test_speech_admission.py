"""Admission control: the retractable tail of a live speech transaction.

The invariant under test is that only text the provider already received is
irreversible.  Everything after that frontier stays local, so a later LLM
decision can replace it without cutting audible playback and without opening a
second TTS generation.
"""

import asyncio
from types import SimpleNamespace

from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.controller.speech_pipeline import AdmissionState
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_admission_state_replaces_only_unsent_frames():
    state = AdmissionState(lead_ms=1000)
    state.sent_chars = 4
    state.push("尾巴甲")
    state.push("尾巴乙")
    assert state.staged_chars == 6

    dropped = state.replace_unsent("修正尾巴")
    assert dropped == 6
    assert list(state.staged) == ["修正尾巴"]
    assert state.staged_chars == 4
    # The irreversible prefix is untouched and the epoch invalidates a frame
    # the sender may already hold.
    assert state.sent_chars == 4
    assert state.epoch == 1


def test_admission_state_refuses_replacement_after_close():
    state = AdmissionState(lead_ms=1000)
    state.push("未送出")
    state.close()
    assert state.rewritable is False
    assert state.replace_unsent("修正") == -1
    assert list(state.staged) == ["未送出"]


def test_admission_state_reports_overrun_against_the_staging_budget():
    state = AdmissionState(lead_ms=1000, char_limit=10)
    state.push("x" * 9)
    assert state.overrun is False
    state.push("x" * 2)
    assert state.overrun is True
    # Popping the staged tail brings the budget back under the limit.
    state.pop()
    assert state.overrun is False


def _output_harness(scheduling, admission_lead_ms):
    """Build a minimal EpisodeSpeechOutput with a controllable player/session."""

    class Session:
        bidi = True
        reusable = True
        generation_finished = True
        completed_segments = 0

        def __init__(self):
            self.received_bytes = 0
            self.sent = []
            # Bytes "returned" by each successive provider chunk.  An empty
            # plan means the provider never answers and the transaction is
            # closed by ``finish``.
            self.audio_plan = []
            self.audio_delay = 0.02
            self.finish_requested = asyncio.Event()

        async def start(self):
            return

        async def send_text(self, text):
            self.sent.append(text)

        async def audio_chunks(self):
            while self.audio_plan:
                await asyncio.sleep(self.audio_delay)
                size = self.audio_plan.pop(0)
                self.received_bytes += size
                yield b"\x00" * min(size, 2048)
            await self.finish_requested.wait()

        async def finish(self):
            self.finish_requested.set()

        async def close(self):
            return

    class Tts:
        transport = "websocket_bidi"
        effective_scheduling = scheduling

        def __init__(self):
            self.admission_lead_ms = admission_lead_ms
            self.session_instance = Session()

        def session(self, fresh=False):
            return self.session_instance

    class Player:
        sample_rate = 1000
        channels = 1

        def __init__(self):
            self.played_ms = 0
            self.enqueued = []

        @property
        def buffered_ms(self):
            return 0

        def allow(self, key):
            return None

        def enqueue(self, key, pcm):
            self.enqueued.append((key, pcm))

        def progress(self, key):
            return {
                "key": key, "enqueued_ms": 0, "played_ms": self.played_ms,
                "buffered_ms": 0, "started": True,
                "output_estimated_ms": self.played_ms, "device_output_time": None,
            }

        async def wait_until_output_complete(self, timeout_seconds=10):
            return

    async def emit(*_args, **_kwargs):
        return None

    async def log(*_args, **_kwargs):
        return None

    tts, player = Tts(), Player()
    runtime = SimpleNamespace(
        gate=lambda _identity: None, emit=emit, log=log,
        user_speaking=False, closed=False, request_started_at=None,
    )
    controller = SimpleNamespace(
        tts=tts, player=player,
        options=SimpleNamespace(tts_pacing=False, tts_lead_ms=1, tts_local_buffer_ms=1),
        bridge_options={}, _turn_timings={}, _track=lambda task: task,
    )
    output = EpisodeSpeechOutput(controller)
    output.runtime = runtime
    output.loop = asyncio.get_running_loop()
    handle = SpeechHandle(output, ("episode", "request"), "turn", 0, "calm")
    handle.playback_ready.set()  # 此夹具表示已获端点播放授权。
    handle.task = asyncio.current_task()
    output.handle = handle
    output._handles_by_token[handle.workspace.token] = handle
    return output, handle, tts, player


def test_lead_window_holds_text_until_playback_catches_up():
    async def scenario():
        output, handle, tts, player = _output_harness("playback_window", 500)
        session = tts.session_instance
        # 1000 ms of already-returned audio while nothing has been played yet.
        session.received_bytes = 2000

        async def deltas():
            yield "甲段"
            await asyncio.sleep(0.06)
            yield "乙段"
            await asyncio.sleep(0.06)
            yield None

        running = asyncio.create_task(
            output._play_stream(handle.workspace, handle.workspace.token, deltas(),
                                speech_id=handle.speech_id, handle=handle)
        )
        await asyncio.sleep(0.3)
        # The window is closed by the provider backlog, so only the first
        # frame was released while the rest stayed locally replaceable.
        assert session.sent == ["甲段"]
        assert output._tts_session is session

        player.played_ms = 900
        session.finish_requested.set()
        await asyncio.wait_for(running, timeout=5)
        assert session.sent == ["甲段", "乙段"]

    asyncio.run(scenario())


def test_window_stays_closed_until_the_provider_answers():
    """Regression: a stale ``returned_ms`` must not open the window for a burst.

    At the start of an answer nothing has been returned yet, so a window that
    only compares returned audio against playback reads ``remaining_ms == 0``
    and releases the entire response in one burst.  The sender must wait for
    the provider to answer the frame it just sent before re-measuring.
    """

    async def scenario():
        output, handle, tts, player = _output_harness("playback_window", 500)
        session = tts.session_instance
        # 2000 bytes per second of audio: each 10000-byte chunk is ~5000 ms,
        # far above the 500 ms lead window.  The first chunk is deliberately
        # slow so an unguarded sender would release several frames during the
        # stale window.
        session.audio_plan = [10000] * 20
        session.audio_delay = 0.2
        frames = 10

        async def deltas():
            for index in range(frames):
                yield f"第{index}段"
                await asyncio.sleep(0.04)
            yield None

        running = asyncio.create_task(
            output._play_stream(handle.workspace, handle.workspace.token, deltas(),
                                speech_id=handle.speech_id, handle=handle)
        )
        await asyncio.sleep(0.6)
        # Playback has not advanced, so only the frame whose audio is already
        # outstanding may have been released.
        assert session.sent == ["第0段"], session.sent
        assert handle.admission.staged_chars > 0

        player.played_ms = 10 ** 9
        session.finish_requested.set()
        await asyncio.wait_for(running, timeout=30)
        assert len(session.sent) == frames

    asyncio.run(scenario())


def test_staged_text_never_blocks_the_producer():
    async def scenario():
        output, handle, tts, player = _output_harness("playback_window", 500)
        session = tts.session_instance
        # A permanently closed window: playback never advances.  The frame
        # count deliberately exceeds the 32-frame bound of the previous
        # blocking queue, which would have stalled the producer here.
        frames = 40
        session.received_bytes = 20000
        consumed = []

        async def deltas():
            for index in range(frames):
                consumed.append(index)
                yield f"第{index}段"
                await asyncio.sleep(0.04)
            yield None

        running = asyncio.create_task(
            output._play_stream(handle.workspace, handle.workspace.token, deltas(),
                                speech_id=handle.speech_id, handle=handle)
        )
        await asyncio.sleep(2.5)
        # Every delta was accepted even though the admission window never
        # opened: staging must not propagate the playback clock back into the
        # LLM reader.
        assert consumed == list(range(frames))
        assert session.sent == ["第0段"]
        state = handle.admission
        assert state is not None and state.staged_chars > 0

        player.played_ms = 20000
        session.finish_requested.set()
        await asyncio.wait_for(running, timeout=20)
        assert len(session.sent) == frames

    asyncio.run(scenario())


def test_rewrite_unsent_splices_the_tail_and_truncates_accepted_text():
    async def scenario():
        output, handle, _tts, _player = _output_harness("playback_window", 500)
        state = AdmissionState(lead_ms=500)
        state.producer_done = True
        handle.admission = state
        handle.finished = True
        handle.text = "前半句还会继续"
        state.sent_chars = 3
        state.push("还会继续")

        # ``text`` is the new tail only: the caller never reproduces the
        # locked prefix, so the runtime stitches it locally.
        rewritten = await output.rewrite_unsent(
            ("episode", "request"), "turn", speech_id=handle.speech_id,
            expected_generation=handle.workspace.generation,
            expected_sent_chars=output.speak_snapshot["sent_chars"],
            expected_text=output.speak_snapshot["text"], text="改了口",
        )
        assert rewritten is handle
        assert handle.text == "前半句改了口"
        assert state.sent_chars == 3
        assert list(state.staged) == ["改了口"]

        # The same speech transaction, generation and playback key survive.
        assert output.handle is handle
        assert handle.workspace.generation == 0

    asyncio.run(scenario())


def test_rewrite_unsent_replaces_the_tail_without_comparing_prefixes():
    async def scenario():
        output, handle, _tts, _player = _output_harness("playback_window", 500)
        state = AdmissionState(lead_ms=500)
        state.producer_done = True
        handle.admission = state
        handle.finished = True
        handle.text = "已经送出的内容"
        state.sent_chars = 4
        state.push("的内容")

        # A tail that does not extend the locked prefix is still a legal
        # revision: only the unsent remainder is dropped and replaced.
        rewritten = await output.rewrite_unsent(
            ("episode", "request"), "turn", speech_id=handle.speech_id,
            expected_generation=handle.workspace.generation,
            expected_sent_chars=output.speak_snapshot["sent_chars"],
            expected_text=output.speak_snapshot["text"], text="完全改口了",
        )
        assert rewritten is handle
        assert handle.text == "已经送出" + "完全改口了"
        assert state.sent_chars == 4
        assert list(state.staged) == ["完全改口了"]

    asyncio.run(scenario())


def test_rewrite_unsent_requires_an_open_unfinished_transaction():
    async def scenario():
        output, handle, _tts, _player = _output_harness("playback_window", 500)
        # 尚未进入管线，没有已提交正文；缺少可改队列时也不能执行尾巴修订。
        handle.finished = True
        handle.text = "旧内容"
        assert handle.sent_chars == 0
        assert await output.rewrite_unsent(
            ("episode", "request"), "turn", speech_id=handle.speech_id,
            expected_generation=handle.workspace.generation,
            expected_sent_chars=output.speak_snapshot["sent_chars"],
            expected_text=output.speak_snapshot["text"], text="旧内容续写",
        ) is None

        state = AdmissionState(lead_ms=500)
        state.producer_done = True
        handle.admission = state
        handle.finished = False
        assert handle.rewritable is False
        assert await output.rewrite_unsent(
            ("episode", "request"), "turn", speech_id=handle.speech_id,
            expected_generation=handle.workspace.generation,
            expected_sent_chars=output.speak_snapshot["sent_chars"],
            expected_text=output.speak_snapshot["text"], text="旧内容续写",
        ) is None

    asyncio.run(scenario())


def test_streamed_speak_cannot_rewrite_existing_tail():
    """流式 speak（新播报）不能接管现有事务的尾巴。"""

    async def scenario():
        seen = {"rewrite": [], "replace": 0}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content",
                           "text": '{"speak":{"state":"speak","controls":[],"text":"已有改'}
                    yield {"type": "content", "text": '正"},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "live_speech"

            def __init__(self, text):
                self.text = text

            async def feed(self, value): seen.setdefault("feeds", []).append(value)
            async def finish(self): pass

        class Output:
            status = "SPEAKING"
            speak_snapshot = {}

            async def rewrite_unsent(self, identity, turn_id, *, speech_id, text):
                # ``text`` is the new tail; a live transaction stitches it onto
                # its own locked prefix.
                seen["rewrite"].append((speech_id, text))
                return Handle("已有" + text)

            async def replace_current(self, *_args, **_kwargs):
                seen["replace"] += 1
                return Handle("replacement")

        async def emit(*_args, **_kwargs): return None

        output = Output()
        conversation = ConversationRuntime()
        conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), output, emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        # Only the first two characters reached the provider.
        output.speak_snapshot = {
            "queue_status": "speaking", "speech_id": "live_speech",
            "conversation_id": runtime.episode_id, "turn_id": "turn",
            "output_generation": 3, "text": "已有播报",
            "sent_chars": 2, "rewritable": True,
        }
        identity = (runtime.episode_id, "request")
        runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"

        await runtime._step(identity, {"USER_INPUT_REFINED"})

        assert seen["rewrite"] == []
        assert "feeds" not in seen
        assert runtime.minor_protocol_feedback[-1]["applied"] == "ignored_output_busy"
        assert seen["replace"] == 0

    asyncio.run(scenario())


def _runtime_with_output(output):
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []

    async def emit(*_args, **_kwargs):
        return None

    conversation = ConversationRuntime()
    conversation.begin_turn("turn")
    runtime = AgentEpisodeRuntime(None, Registry(), output, emit,
                                  conversation=conversation, ledger=SessionLedger())
    runtime.episode_id = conversation.conversation_id
    return runtime, (runtime.episode_id, "request")


def test_complete_speak_cannot_rewrite_existing_tail():
    """完整 speak（新播报）即使有未提交尾巴也必须拒绝。"""

    async def scenario():
        seen = {"rewrite": [], "replace": [], "cancel": []}

        class Handle:
            speech_id = "live_speech"
            async def feed(self, text): seen["feeds"] = text
            async def finish(self): pass

        class Output:
            active = True
            status = "SPEAKING"
            speak_snapshot = {}

            def __init__(self):
                self.snapshot_text = "概括一下重点内容"
                self.snapshot_sent = 4
                self.snapshot_rewritable = True

            async def rewrite_unsent(self, identity, turn_id, *, speech_id, text):
                seen["rewrite"].append((speech_id, text))
                return Handle()
            async def replace_current(self, *_args, **kwargs):
                seen["replace"].append(kwargs)
                return Handle()
            def cancel_all(self, reason): seen["cancel"].append(reason)

        output = Output()
        runtime, identity = _runtime_with_output(output)
        # The provider has 4 of the 9 accepted characters; the rest is still
        # local and therefore replaceable.
        output.speak_snapshot = {
            "queue_status": "speaking", "speech_id": "live_speech",
            "conversation_id": identity[0], "turn_id": "turn",
            "text": output.snapshot_text, "sent_chars": output.snapshot_sent,
            "rewritable": output.snapshot_rewritable,
        }
        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", "概括一下修正内容"), ())
        await runtime._apply_decision_v06(identity, "turn", envelope)

        # Only the part beyond the four locked characters is offered.
        assert seen["rewrite"] == []
        assert seen["replace"] == [] and seen["cancel"] == []

    asyncio.run(scenario())


def test_divergent_speak_cannot_hard_replace():
    """Content the provider already received cannot be rewritten in place."""

    async def scenario():
        seen = {"rewrite": [], "replace": []}

        class Handle:
            speech_id = "new_speech"
            kind = "formal"
            async def feed(self, text): seen["feeds"] = text
            async def finish(self): pass

        class Output:
            active = True
            status = "SPEAKING"
            speak_snapshot = {
                "queue_status": "speaking", "speech_id": "live_speech",
                "text": "概括一下重点内容", "sent_chars": 9, "rewritable": True,
            }
            async def rewrite_unsent(self, identity, turn_id, *, speech_id, text):
                seen["rewrite"].append(text)
                return None  # the caller's own guard already rejects this case
            async def replace_current(self, *_args, **kwargs):
                seen["replace"].append(kwargs)
                return Handle()
            def cancel_all(self, reason): seen.setdefault("cancel", []).append(reason)

        runtime, identity = _runtime_with_output(Output())
        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", "完全不同的一整句"), ())
        await runtime._apply_decision_v06(identity, "turn", envelope)

        # Divergence falls inside the irreversible prefix, so the decision
        # never even offers a rewrite and keeps the existing replacement path.
        assert seen["rewrite"] == []
        assert seen["replace"] == []
        assert "feeds" not in seen

    asyncio.run(scenario())


def test_controller_revision_confirms_boundary_and_emits_authoritative_text():
    async def scenario():
        output, handle, _tts, _player = _output_harness("playback_window", 500)
        handle.finished = True
        handle.text = "前缀旧尾巴"
        state = AdmissionState(lead_ms=500)
        state.producer_done = True
        state.sent_chars = 2
        state.push("旧尾巴")
        handle.admission = state
        events = []
        async def emit(kind, payload):
            events.append((kind, payload))
        output.runtime.emit = emit
        frozen = dict(output.speak_snapshot)
        state.commit_segment("旧")
        assert await output.rewrite_unsent(
            handle.identity, handle.turn_id, speech_id=handle.speech_id,
            text="新尾巴", expected_generation=0, expected_sent_chars=frozen["sent_chars"],
            expected_text=frozen["text"]) is None
        assert handle.text == "前缀旧尾巴" and not events
        frozen = dict(output.speak_snapshot)
        assert await output.rewrite_unsent(
            (handle.identity[0], "followup-request"), "followup-turn", speech_id=handle.speech_id,
            text="改正", expected_generation=0, expected_sent_chars=frozen["sent_chars"],
            expected_text=frozen["text"]) is handle
        assert handle.text == "前缀旧改正"
        assert output.state.current["assistant_text"] == handle.text
        assert handle.workspace.staged_response == handle.text
        assert events[0][0] == "speech.tail_rewritten"
        assert events[0][1]["text"] == handle.text
        assert events[0][1]["text_revision"] == 1
        assert events[0][1]["turn_id"] == handle.turn_id
        assert events[0][1]["revision_source_turn_id"] == "followup-turn"
        assert events[0][1]["revision_request_id"] == "followup-request"
        # 相同提交前缀也不能接受针对旧版本生成的第二次修订。
        assert await output.rewrite_unsent(
            handle.identity, handle.turn_id, speech_id=handle.speech_id,
            text="再次修改", expected_generation=0, expected_sent_chars=frozen["sent_chars"],
            expected_text=handle.text, expected_text_revision=0) is None
        # 同一旧事务的快照即使仍吻合，也不能跨过真正的输出代次撤销。
        output.generation += 1
        assert await output.rewrite_unsent(
            handle.identity, "another-turn", speech_id=handle.speech_id,
            text="过期结果", expected_generation=0, expected_sent_chars=handle.sent_chars,
            expected_text=handle.text, expected_text_revision=1) is None
        assert handle.text == "前缀旧改正"
    asyncio.run(scenario())


def test_tts_send_in_flight_closes_revision_window_until_commit():
    async def scenario():
        output, handle, tts, player = _output_harness("direct", 0)
        handle.finished = True
        handle.text = "原句。"
        entered, release = asyncio.Event(), asyncio.Event()
        session = tts.session_instance
        original_send = session.send_text
        async def blocked_send(text):
            entered.set()
            await release.wait()
            await original_send(text)
        session.send_text = blocked_send
        async def text_stream():
            yield handle.text
        task = asyncio.create_task(output._play_stream(handle.workspace, handle.workspace.token, text_stream(),
                                                       speech_id=handle.speech_id, handle=handle))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert handle.admission.sending
            assert not handle.rewritable
            frozen = output.speak_snapshot
            assert await output.rewrite_unsent(
                handle.identity, handle.turn_id, speech_id=handle.speech_id,
                text="不能抢改", expected_generation=0,
                expected_sent_chars=frozen["sent_chars"], expected_text=frozen["text"]) is None
            release.set()
            session.finish_requested.set()
            await asyncio.wait_for(task, 5)
            assert session.sent == ["原句。"]
            assert handle.admission.sent_chars == len("原句。")
            assert not handle.admission.sending
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_rewrite_event_is_part_of_public_session_protocol():
    from voice_terminal.core.events import VoiceEvent
    from voice_terminal.server.snapshot import ProtocolSequencer
    event = VoiceEvent(type="speech.tail_rewritten", turn_id="turn",
                       payload={"speech_id": "live", "text": "新文本", "text_revision": 1})
    sequencer = ProtocolSequencer("conversation")
    assert sequencer.public(event)
    assert sequencer.envelope(event)["payload"]["text"] == "新文本"


def test_revision_waits_until_old_local_text_has_finished_staging():
    async def scenario():
        output, handle, tts, _player = _output_harness("direct", 0)
        handle.finished = True
        handle.text = "旧尾巴仍在本地分段。"
        entered, release = asyncio.Event(), asyncio.Event()
        async def detail(kind, **_payload):
            if kind == "assistant.delta":
                entered.set()
                await release.wait()
        output._detail = detail
        async def text_stream():
            yield handle.text
        task = asyncio.create_task(output._play_stream(
            handle.workspace, handle.workspace.token, text_stream(),
            speech_id=handle.speech_id, handle=handle))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert handle.finished and not handle.admission.producer_done
            assert not handle.rewritable
            frozen = output.speak_snapshot
            assert await output.rewrite_unsent(
                handle.identity, handle.turn_id, speech_id=handle.speech_id,
                text="新尾巴", expected_generation=0,
                expected_sent_chars=frozen["sent_chars"], expected_text=frozen["text"]) is None
            release.set()
            tts.session_instance.finish_requested.set()
            await asyncio.wait_for(task, 5)
            assert tts.session_instance.sent == ["旧尾巴仍在本地分段。"]
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
