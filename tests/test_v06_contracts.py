from pathlib import Path
import asyncio
import json
from types import SimpleNamespace

import pytest

import voice_terminal.agent.episode as episode_module
from voice_terminal.agent.decision_envelope import (
    DecisionEnvelope,
    EnvelopeError,
    SpeechDecision,
    SpeechDecisionEnvelopeStream,
)
from voice_terminal.agent.episode import AgentDecisionScheduler, AgentEpisodeRuntime
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.sentences import SentenceStore, map_refinement
from voice_terminal.config import AppConfig, load_config
from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.speech_pipeline import SpeechPipeline
from voice_terminal.core.state import RuntimeState
from voice_terminal.audio.player import PlaybackSnapshot
from voice_terminal.core.events import event
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.server.snapshot import ProtocolSequencer
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_json_mode_and_low_thinking_payload_are_explicit():
    client = DeepSeekClient(base_url="https://example.invalid", model="deepseek-v4-flash", api_key="key", thinking="enabled")
    payload = client._request_payload([{"role": "user", "content": "你好"}])
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"


def test_v06_envelope_stream_and_final_validation():
    parser = SpeechDecisionEnvelopeStream()
    assert parser.feed('{"actions":[],"speak":{"text":"先到的') == ""
    assert parser.feed('文本","state":"speak","emotion":"calm"}}') == "先到的文本"
    decision = parser.finish()
    assert decision.speak.state == "speak" and decision.speak.text == "先到的文本"
    with pytest.raises(EnvelopeError):
        bad = SpeechDecisionEnvelopeStream(); bad.feed('{"speak":{"state":"wait","text":"不允许"},"actions":[]}'); bad.finish()


def test_v06_envelope_exposes_only_a_display_preview_before_final_validation():
    parser = SpeechDecisionEnvelopeStream()
    assert parser.feed('{"speak":{"state":"speak","text":"流式') == "流式"
    assert parser.feed('预览"},"actions":[]}') == "预览"
    assert parser.finish().speak.text == "流式预览"


def test_v06_prefix_is_safe_before_full_envelope_and_validates_emotion():
    parser = SpeechDecisionEnvelopeStream()
    assert parser.feed('{"speak":{"state":"speak","emotion":"calm","text":"首段') == "首段"
    prefix = parser.speak_prefix()
    assert prefix is not None and prefix.state == "speak" and prefix.emotion == "calm"
    with pytest.raises(EnvelopeError, match="invalid speak emotion"):
        bad = SpeechDecisionEnvelopeStream()
        bad.feed('{"speak":{"state":"speak","emotion":"unknown","text":"x"}')


def test_real_playback_start_emits_one_observational_event():
    async def scenario():
        emitted = []
        logged = []
        tracked = []

        class Runtime:
            async def emit(self, kind, payload):
                emitted.append((kind, payload))

            async def log(self, kind, **payload):
                logged.append((kind, payload))

        output = object.__new__(EpisodeSpeechOutput)
        output.loop = asyncio.get_running_loop()
        output.runtime = Runtime()
        output.player = SimpleNamespace(diagnostics={"started": True})
        output.controller = SimpleNamespace(
            state=SimpleNamespace(current={}),
            _turn_timings={"turn_a": {}},
            _track=lambda task: tracked.append(task),
        )
        output.handle = SimpleNamespace(
            speech_id="speech_a",
            identity=("conversation_a", "request_a"),
            turn_id="turn_a",
            reason=None,
            text="正在播放",
            kind="formal",
            emotion_requested="calm",
            emotion_applied="calm",
            workspace=SimpleNamespace(
                playback_key="key_a", playback_started=False, generation=7, timings={},
            ),
        )

        output.playback_started("stale_key")
        output.playback_started("key_a")
        output.playback_started("key_a")
        await asyncio.sleep(0)
        await asyncio.gather(*tracked)

        assert output.handle.workspace.playback_started is True
        assert [kind for kind, _ in emitted] == ["speech.started"]
        assert [kind for kind, _ in logged] == ["TTS_STARTED"]
        assert emitted[0][1] == {
            "speech_id": "speech_a",
            "conversation_id": "conversation_a",
            "request_id": "request_a",
            "trace_id": "request_a",
            "turn_id": "turn_a",
            "output_generation": 7,
            "text": "正在播放",
            "status": "playing",
            "kind": "formal",
            "emotion_requested": "calm",
            "emotion_applied": "calm",
        }

    asyncio.run(scenario())


def test_subscription_snapshot_uses_live_episode_without_rewriting_cached_agent():
    runtime = object.__new__(SessionRuntime)
    runtime.state = RuntimeState()
    runtime.state.agent = {"conversation_id": "stale", "speak_snapshot": {"queue_status": "speaking"}}
    live = {"conversation_id": "live", "speak_snapshot": {"queue_status": "empty"}}
    runtime.controller = SimpleNamespace(episode=SimpleNamespace(snapshot=lambda **_: live))
    runtime.history = SimpleNamespace(records=[])
    runtime.reasoning_mode = "balanced"

    payload = runtime.snapshot().payload

    assert payload["agent"] == live
    assert runtime.state.agent["conversation_id"] == "stale"


def test_subscription_snapshot_keeps_cached_agent_without_controller():
    runtime = object.__new__(SessionRuntime)
    runtime.state = RuntimeState()
    runtime.state.agent = {"conversation_id": "cached"}
    runtime.controller = None
    runtime.history = SimpleNamespace(records=[])
    runtime.reasoning_mode = "balanced"

    assert runtime.snapshot().payload["agent"] == {"conversation_id": "cached"}


def test_subscription_snapshot_falls_back_when_live_episode_snapshot_fails():
    runtime = object.__new__(SessionRuntime)
    runtime.state = RuntimeState()
    runtime.state.agent = {"conversation_id": "cached"}

    def failing_snapshot(**_kwargs):
        raise RuntimeError("episode is being initialized")

    runtime.controller = SimpleNamespace(episode=SimpleNamespace(snapshot=failing_snapshot))
    runtime.history = SimpleNamespace(records=[])
    runtime.reasoning_mode = "balanced"

    assert runtime.snapshot().payload["agent"] == {"conversation_id": "cached"}


def test_friendly_emotion_is_safely_normalized_and_remains_observable():
    parser = SpeechDecisionEnvelopeStream()
    assert parser.feed('{"speak":{"state":"speak","emotion":"friendly","text":"你好"') == "你好"
    prefix = parser.speak_prefix()
    assert prefix is not None and prefix.emotion == "calm"
    parser.feed('},"actions":[]}')
    assert parser.finish().speak.emotion == "calm"
    assert parser.emotion_normalizations == [{"provided": "friendly", "applied": "calm"}]


def test_minor_emotion_repair_is_declared_on_the_next_llm_poll():
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []

    async def emit(*_): pass

    runtime = AgentEpisodeRuntime(None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
                                  conversation=ConversationRuntime(), ledger=SessionLedger())
    runtime.last_turn_id = "turn_a"
    runtime.inputs["turn_a"] = {"text": "你好", "final": True}
    runtime.minor_protocol_feedback.append({
        "field": "speak.emotion", "provided": "friendly", "applied": "calm",
    })

    messages = runtime.messages({"USER_INPUT_FINALIZED"})

    assert any("轻度协议偏差" in message["content"] and "friendly" in message["content"]
               for message in messages)
    assert not runtime.minor_protocol_feedback


def test_model_context_keeps_speech_semantics_but_drops_scheduler_diagnostics():
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []

    async def emit(*_): pass

    output = SimpleNamespace(speak_snapshot={
        "queue_status": "speaking", "text_only_queue": True,
        "text": "已经播出的开头", "speech_id": "local_speech_id",
        "output_generation": 7, "emotion": "calm",
        "delivery": {"heard_text": "已经播出"},
    })
    runtime = AgentEpisodeRuntime(None, Registry(), output, emit,
                                  conversation=ConversationRuntime(), ledger=SessionLedger())
    runtime.last_turn_id = "turn_a"
    runtime.inputs["turn_a"] = {"text": "继续说", "final": False, "sentences": [{"sentence_id": 1}]}
    runtime.event_inbox.append({"event_id": "internal_uuid", "occurred_at": 1})

    messages = runtime.messages({"USER_SEGMENT_UPDATED"})
    rendered = "\n".join(message["content"] for message in messages)
    assert "当前轮运行状态" in rendered
    assert "已经播出的开头" in rendered and "已经播出" in rendered
    assert "local_speech_id" not in rendered
    assert "output_generation" not in rendered
    assert "internal_uuid" not in rendered
    assert "input_waiting_ms" not in rendered


def test_sentence_ids_stay_stable_and_unmapped_refinement_is_retained():
    store = SentenceStore(min_chars=2, max_chars=20)
    first = store.feed("你好，世界。", now=1)
    assert first and first["changed_ids"] == [1, 2]
    second = store.feed("你好，世界。再见。", now=2)
    assert second and [row["sentence_id"] for row in second["sentences"]] == [1, 2, 3]
    assert map_refinement(second["sentences"], "完全不同的精修结果")["unmapped_refinement"]


def test_scheduler_defaults_and_removed_prediction_configuration():
    config = load_config(Path("config/default.yaml"))
    assert config.agent.scheduler_tick_ms == 400 and config.agent.voice_activation_ms == 200
    assert config.llm.max_output_tokens == 384 * 1024
    assert AgentDecisionScheduler(AgentOptions()).accepts("TOOL_COMPLETED")
    with pytest.raises(TypeError, match="predictions"):
        AppConfig.from_mapping({"smart_turn": {"predictions": True}})


def test_passive_speech_activity_keeps_llm_and_thinking_state_alive():
    async def scenario():
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Output:
            def __init__(self):
                self.held = 0
                self.released = 0
                self.cancelled = []
                self.status = "IDLE"
                self.active_snapshot = None
                self.queue_snapshot = []
                self.speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def hold_unplayed(self):
                self.held += 1

            def release_held(self):
                self.released += 1

            def cancel_all(self, reason):
                self.cancelled.append(reason)

        async def emit(*_args): pass

        conversation = ConversationRuntime()
        conversation.begin_turn("turn_a")
        output = Output()
        runtime = AgentEpisodeRuntime(
            None, Registry(), output, emit,
            conversation=conversation, ledger=SessionLedger(),
        )
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn_a"
        runtime.current_action = "speak"
        runtime._thinking_barrier_generation = 7
        runtime._wake = lambda _kind: None
        request = asyncio.create_task(asyncio.sleep(60))
        runtime.active_request = request
        try:
            await runtime.speech_activity(True)
            assert runtime.user_speaking is True
            assert output.held == 1
            assert output.cancelled == []
            assert not request.cancelled()
            assert runtime.current_action == "speak"
            assert runtime._thinking_barrier_generation == 7

            await runtime.speech_activity(False)
            assert runtime.user_speaking is False
            assert output.released == 1
            assert not request.cancelled()
        finally:
            if runtime.voice_activation_task and not runtime.voice_activation_task.done():
                runtime.voice_activation_task.cancel()
                await asyncio.gather(runtime.voice_activation_task, return_exceptions=True)
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())


def test_stopping_listening_does_not_cancel_the_episode_output_scope(monkeypatch):
    async def scenario():
        calls = []

        class Episode:
            closed = False

            async def speech_activity(self, speaking, **kwargs):
                calls.append(("speech_activity", speaking, kwargs))

            async def stop(self):
                raise AssertionError("stopping input must not close the Agent episode")

        class Tts:
            async def close(self):
                raise AssertionError("stopping input must not close the shared TTS client")

        async def cancel_prewarm(*_args, **_kwargs):
            calls.append(("cancel_prewarm",))

        async def base_stop(self):
            calls.append(("base_stop",))

        monkeypatch.setattr(RealtimeController, "stop_listening", base_stop)
        controller = object.__new__(EpisodeController)
        controller.state = SimpleNamespace(listening=True)
        controller.workspace = None
        controller.episode = Episode()
        controller.tts = Tts()
        controller._cancel_tts_prewarm = cancel_prewarm

        await EpisodeController.stop_listening(controller)

        assert controller.state.listening is False
        assert calls == [
            ("speech_activity", None, {}),
            ("cancel_prewarm",),
            ("base_stop",),
        ]

    asyncio.run(scenario())


def test_explicit_barge_in_cancels_after_passive_onset_even_if_state_is_unchanged():
    async def scenario():
        class Output:
            def __init__(self):
                self.cancelled = []
                self.status = "IDLE"
                self.active_snapshot = None
                self.queue_snapshot = []
                self.speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def hold_unplayed(self): pass
            def cancel_all(self, reason): self.cancelled.append(reason)

        async def emit(*_args): pass

        output = Output()
        runtime = AgentEpisodeRuntime(
            None, SimpleNamespace(), output, emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime._wake = lambda _kind: None
        await runtime.speech_activity(True)
        request = asyncio.create_task(asyncio.sleep(60))
        runtime.active_request = request
        try:
            await runtime.speech_activity(True, interrupt_reason="user_interrupted")
            await asyncio.sleep(0)
            assert request.cancelled()
            assert output.cancelled == ["interrupted"]
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if runtime.voice_activation_task and not runtime.voice_activation_task.done():
                runtime.voice_activation_task.cancel()
                await asyncio.gather(runtime.voice_activation_task, return_exceptions=True)

    asyncio.run(scenario())


def test_first_token_estimate_uses_fallback_while_tokenizer_warms_in_background(monkeypatch):
    """A cold tokenizer cache must never block the realtime event loop."""

    async def scenario():
        calls = []
        monkeypatch.setattr(episode_module, "_O200K_ENCODING", None)
        monkeypatch.setattr(episode_module, "_O200K_STATE", "uninitialized")
        monkeypatch.setattr(episode_module, "_load_o200k_encoding", lambda: calls.append("loaded"))

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        async def emit(*_): pass

        runtime = AgentEpisodeRuntime(None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
                                      conversation=ConversationRuntime(), ledger=SessionLedger())
        await runtime.ensure_episode(turn_id="turn_a", text="你好")

        # The exact encoder is intentionally unavailable until its worker has
        # finished; estimating still returns immediately and deterministically.
        assert runtime._estimate_tokens("中文 hello") == episode_module._fallback_token_estimate("中文 hello")
        await runtime._tokenizer_prewarm_task
        assert calls == ["loaded"]

    asyncio.run(scenario())


def test_debug_events_are_public_protocol_events():
    sequencer = ProtocolSequencer("session_a")
    assert sequencer.public(event("agent.reasoning", "input_a"))
    assert sequencer.public(event("speak.preview", "input_a"))
    assert sequencer.public(event("speech.queued", "input_a"))
    assert sequencer.public(event("shadow.debug.wake", None))
    assert sequencer.public(event("shadow.debug.event", None))
    assert sequencer.public(event("shadow.activity", None, run_seq=1, item_id="run-1", stream="lifecycle", phase="started"))


def test_replacement_delivery_alignment_is_scoped_to_each_speech_id():
    """A replacement must not lose old playback boundaries to the turn key."""

    pipeline = object.__new__(SpeechPipeline)
    pipeline._delivery_resolvers = {}
    workspace = SimpleNamespace(turn_id="turn_a")

    old = pipeline._delivery_resolver_for(workspace, speech_id="speech_old")
    new = pipeline._delivery_resolver_for(workspace, speech_id="speech_new")

    assert old is pipeline._delivery_resolvers["speech_old"]
    assert new is pipeline._delivery_resolvers["speech_new"]
    assert old is not new
    assert "turn_a" not in pipeline._delivery_resolvers


def test_completed_playback_overrides_missing_alignment_and_settles_before_queue_is_idle():
    async def scenario():
        captured = {}

        class Player:
            def stop_and_snapshot(self, key):
                return PlaybackSnapshot(key, 500, 500, 0, 500, True, 1.0)

        async def emit(*_args):
            pass

        async def log(kind, **_payload):
            captured.setdefault("logs", []).append(kind)

        controller = SimpleNamespace(
            tts=SimpleNamespace(
                emotion_status="applied", emotion_transport="voice_setting",
                applied_emotion="calm",
            ),
            player=Player(), options=SimpleNamespace(), bridge_options={},
            _turn_timings={}, state=SimpleNamespace(latency={"values": {}}),
            workspace=None,
            _add_playback_delivery_fields=lambda delivery, snapshot: delivery,
        )
        output = EpisodeSpeechOutput(controller)

        async def output_completed(record):
            assert output.busy is True
            captured["record"] = record

        output.runtime = SimpleNamespace(
            gate=lambda _identity: None, emit=emit, log=log, closed=False,
            request_started_at=None, input_finished_at={}, output_completed=output_completed,
        )
        handle = SpeechHandle(output, ("episode", "request"), "turn", 0, "calm")
        handle.text = "完整回答"
        handle.task = asyncio.current_task()
        output.handle = handle
        output._handles_by_token[handle.workspace.token] = handle
        output._delivery_resolver_for(handle.workspace, speech_id=handle.speech_id)

        async def fake_play(ws, *_args, **_kwargs):
            ws.playback_started = True
            ws.timings.update({
                "tts_text_sent": 1, "tts_audio_bytes": 32000,
                "playback_output_complete": True,
            })
            output.state.current["tts_status"] = "FINISHED"
            # Avoid the real one-second extension window in this focused unit
            # test; the delivery snapshot has already been frozen by _run.
            output.handle = None

        output._play_stream = fake_play
        await output._run(handle)

        delivery = captured["record"]["delivery"]
        assert delivery["status"] == "full"
        assert delivery["heard_text"] == "完整回答"
        assert delivery["precision"] == "playback_complete"
        assert delivery["reason"] == "completed_playback_authoritative"
        assert captured["logs"][-2:] == ["SPEECH_SETTLING_STARTED", "SPEECH_SETTLING_FINISHED"]
        assert output.busy is False

    asyncio.run(scenario())


def test_refinement_is_supplemental_and_never_changes_cancellation_scope():
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []
    async def emit(*_): pass
    conversation = ConversationRuntime()
    runtime = AgentEpisodeRuntime(None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
                                  conversation=conversation, ledger=SessionLedger())
    wakes = []; runtime._wake = lambda kind: wakes.append(kind)
    asyncio.run(runtime.observe("USER_INPUT_FINALIZED", turn_id="turn_a", text="明天开会"))
    scope = conversation.scope_id
    asyncio.run(runtime.observe("USER_INPUT_REFINED", turn_id="turn_a", text="明天下午开会"))
    assert conversation.scope_id == scope and wakes[-1] == "USER_INPUT_REFINED"


def test_late_refinement_keeps_newest_turn_current_but_targets_original_turn_for_dispatch():
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []

    async def emit(*_): pass

    runtime = AgentEpisodeRuntime(
        None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )
    runtime.episode_id = "conversation-a"
    runtime.inputs = {
        "old": {"text": "旧轮", "final": True, "transcript_updates": []},
        "new": {"text": "新轮", "final": True, "transcript_updates": []},
    }
    runtime.last_turn_id = "new"
    wakes = []
    runtime._wake = lambda kind: wakes.append(kind)

    asyncio.run(runtime.observe(
        "USER_INPUT_REFINED", turn_id="old", text="旧轮精修",
        source="sensevoice", source_revision=1,
    ))

    assert runtime.last_turn_id == "new"
    assert runtime._pending_transcript_turn_id == "old"
    assert runtime.inputs["old"]["refined_text"] == "旧轮精修"
    assert wakes == ["USER_INPUT_REFINED"]


def test_same_text_asr_final_is_a_visible_same_turn_update_and_wakes_scheduler():
    async def scenario():
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        async def emit(*_args): pass

        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.episode_id = "conversation-a"
        wakes = []
        runtime._wake = lambda kind: wakes.append(kind)

        await runtime.observe("USER_SEGMENT_UPDATED", turn_id="turn-a", text="给自己起个名字吧")
        wakes.clear()

        await runtime.observe(
            "USER_INPUT_FINALIZED", turn_id="turn-a", text="给自己起个名字吧",
            source="paraformer", source_revision=1,
        )

        current = runtime.inputs["turn-a"]
        assert current["final"] is True
        assert current["transcript_updates"] == [{
            "kind": "asr_final", "source": "paraformer",
            "text": "给自己起个名字吧", "revision": 1,
            "streamed_text": "给自己起个名字吧",
        }]
        assert wakes == ["USER_INPUT_FINALIZED"]

    asyncio.run(scenario())


def test_same_source_revision_is_idempotent_without_semantic_comparison():
    async def scenario():
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        async def emit(*_args): pass

        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(active=False, status="IDLE"), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.episode_id = "conversation-a"
        wakes = []
        runtime._wake = lambda kind: wakes.append(kind)

        kwargs = {"source": "paraformer", "source_revision": 7}
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="给自己起个名字吧", **kwargs)
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="内容即使不同也属重复回调", **kwargs)

        assert wakes == ["USER_INPUT_FINALIZED"]
        assert runtime.inputs["turn-a"]["text"] == "给自己起个名字吧"

    asyncio.run(scenario())


def test_changed_asr_final_keeps_full_text_without_local_relation_classification():
    async def scenario():
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        async def emit(*_args): pass

        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(active=False, status="IDLE", speak_snapshot={}), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.episode_id = "conversation-a"
        wakes = []
        runtime._wake = lambda kind: wakes.append(kind)

        await runtime.observe("USER_SEGMENT_UPDATED", turn_id="turn-a", text="帮我查明天")
        wakes.clear()
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="帮我查明天上海天气")

        current = runtime.inputs["turn-a"]
        assert current["text"] == "帮我查明天上海天气"
        assert current["final"] is True
        assert "asr_final_revision" not in current
        assert current["transcript_updates"][0]["text"] == "帮我查明天上海天气"
        assert current["transcript_updates"][0]["streamed_text"] == "帮我查明天"
        assert wakes == ["USER_INPUT_FINALIZED"]
        messages = runtime.messages({"USER_INPUT_FINALIZED"})
        packet = json.loads(messages[-2]["content"])
        rendered = "\n".join(entry["content"] for entry in packet["entries"])
        assert '"triggered_by":["asr_final"]' in rendered
        assert "帮我查明天上海天气" in rendered

    asyncio.run(scenario())


def test_held_text_and_suspend_do_not_start_tts_before_release():
    async def scenario():
        async def emit(*_): pass
        runtime = SimpleNamespace(gate=lambda _: None, emit=emit, user_speaking=True)
        controller = SimpleNamespace(tts=None, player=None, options=SimpleNamespace(), bridge_options=SimpleNamespace())
        output = EpisodeSpeechOutput(controller); output.runtime = runtime
        ran = asyncio.Event()
        async def fake_run(handle):
            ran.set()
            async for _ in handle.stream(): pass
        output._run = fake_run
        handle = await output.begin(("episode", "request"), "turn")
        await handle.feed("只保存文本"); await handle.finish()
        assert output.speak_snapshot["queue_status"] == "queued"
        output.suspend_pending(); output.release_held()
        await asyncio.sleep(0)
        assert not ran.is_set() and output.speak_snapshot["queue_status"] == "suspended"
        handle.state = "HELD"; output.release_held()
        await handle.task
        assert ran.is_set()
    asyncio.run(scenario())


def test_user_speech_hold_preserves_unplayed_handle_and_releases_it_after_end():
    async def scenario():
        class Player:
            def __init__(self):
                self.held = []
                self.resumed = []

            def hold(self, key):
                self.held.append(key)
                return True

            def resume(self, key):
                self.resumed.append(key)
                return True

        async def emit(*_args): pass
        async def log(*_args, **_kwargs): pass

        player = Player()
        runtime = SimpleNamespace(
            gate=lambda _identity: None,
            emit=emit,
            log=log,
            user_speaking=True,
            closed=False,
            request_started_at=None,
        )
        controller = SimpleNamespace(
            tts=None,
            player=player,
            options=SimpleNamespace(),
            bridge_options=SimpleNamespace(),
            _turn_timings={},
            _track=lambda task: task,
        )
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime
        started = asyncio.Event()

        async def fake_run(handle):
            started.set()
            async for _ in handle.stream():
                pass

        output._run = fake_run
        handle = await output.begin(("episode", "request"), "turn")
        assert handle.state == "HELD"
        assert output.hold_unplayed() == handle.speech_id
        await handle.feed("暂缓播放")
        await handle.finish()
        await asyncio.sleep(0)
        assert not started.is_set()

        output.release_held()
        await handle.task
        assert started.is_set()
        assert player.held == [handle.workspace.playback_key]
        assert player.resumed == [handle.workspace.playback_key]
        assert "playback_hold_started" in handle.workspace.timings
        assert "playback_hold_released" in handle.workspace.timings

    asyncio.run(scenario())


def test_lifecycle_cancel_clears_preplayback_hold_and_player_key():
    async def scenario():
        class Player:
            def __init__(self):
                self.held = []
                self.resumed = []

            def hold(self, key):
                self.held.append(key)
                return True

            def resume(self, key):
                self.resumed.append(key)
                return True

        async def emit(*_args): pass
        async def log(*_args, **_kwargs): pass

        player = Player()
        runtime = SimpleNamespace(
            gate=lambda _identity: None,
            emit=emit,
            log=log,
            user_speaking=True,
            closed=False,
            request_started_at=None,
        )
        controller = SimpleNamespace(
            tts=None,
            player=player,
            options=SimpleNamespace(),
            bridge_options=SimpleNamespace(),
            _turn_timings={},
            _track=lambda task: task,
        )
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime
        handle = await output.begin(("episode", "request"), "turn")
        assert output.hold_unplayed() == handle.speech_id
        assert output.playback_hold_active

        output.cancel_all("input_unavailable")
        await asyncio.gather(handle.task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not output.playback_hold_active
        assert output._playback_release.is_set()
        assert player.resumed == [handle.workspace.playback_key]

    asyncio.run(scenario())


def test_tts_session_started_before_first_pcm_waits_for_user_release():
    async def scenario():
        class Session:
            bidi = False
            reusable = True
            generation_finished = True
            received_bytes = 0

            def __init__(self):
                self.started = asyncio.Event()
                self.start_release = asyncio.Event()
                self.audio_requested = asyncio.Event()
                self.sent = []

            async def start(self):
                self.started.set()
                await self.start_release.wait()

            async def send_text(self, text):
                self.sent.append(text)

            async def audio_chunks(self):
                self.audio_requested.set()
                yield b"\x01\x00"

            async def finish(self):
                return

            async def close(self):
                return

        class Tts:
            transport = "websocket"
            effective_scheduling = "direct"

            def __init__(self):
                self.session_instance = Session()

            def session(self, fresh=False):
                return self.session_instance

        class Player:
            sample_rate = 1000
            channels = 1
            buffered_ms = 0

            def __init__(self):
                self.held = []
                self.resumed = []
                self.enqueued = []

            def allow(self, key): pass

            def hold(self, key):
                self.held.append(key)
                return True

            def resume(self, key):
                self.resumed.append(key)
                return True

            def enqueue(self, key, pcm):
                self.enqueued.append((key, pcm))

            def progress(self, key):
                return {
                    "key": key, "enqueued_ms": 0, "played_ms": 0,
                    "buffered_ms": 0, "started": False,
                    "output_estimated_ms": 0, "device_output_time": None,
                }

            async def wait_until_output_complete(self, timeout_seconds=10):
                return

        async def emit(*_args): pass
        async def log(*_args, **_kwargs): pass

        tts = Tts()
        player = Player()
        runtime = SimpleNamespace(
            gate=lambda _identity: None,
            emit=emit,
            log=log,
            user_speaking=False,
            closed=False,
            request_started_at=None,
        )
        controller = SimpleNamespace(
            tts=tts,
            player=player,
            options=SimpleNamespace(tts_pacing=False, tts_lead_ms=1, tts_local_buffer_ms=1),
            bridge_options={},
            _turn_timings={},
            _track=lambda task: task,
        )
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime
        output.loop = asyncio.get_running_loop()
        handle = SpeechHandle(output, ("episode", "request"), "turn", 0, "calm")
        handle.task = asyncio.current_task()
        output.handle = handle
        output._handles_by_token[handle.workspace.token] = handle

        async def deltas():
            yield "测试"
            yield None

        running = asyncio.create_task(
            output._play_stream(handle.workspace, handle.workspace.token, deltas(), speech_id=handle.speech_id)
        )
        await tts.session_instance.started.wait()
        runtime.user_speaking = True
        assert output.hold_unplayed() == handle.speech_id
        tts.session_instance.start_release.set()
        await asyncio.sleep(0)
        assert tts.session_instance.sent == []
        assert not tts.session_instance.audio_requested.is_set()

        runtime.user_speaking = False
        output.release_held()
        await running
        await asyncio.sleep(0)
        assert tts.session_instance.sent == ["测试"]
        assert tts.session_instance.audio_requested.is_set()
        assert player.enqueued == [(handle.workspace.playback_key, b"\x01\x00")]
        assert player.held == [handle.workspace.playback_key]
        assert player.resumed == [handle.workspace.playback_key]

    asyncio.run(scenario())


def test_runtime_uses_json_mode_and_promotes_only_valid_envelope():
    async def scenario():
        seen = {}
        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                seen["json_mode"] = json_mode
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"好的"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []
        class Handle:
            speech_id = "speech_v06"
            async def feed(self, value): seen["text"] = value
            async def finish(self): seen["finished"] = True
        class Output:
            active = False
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            async def begin(self, *_args, **_kwargs): return Handle()
        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit, conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "turn"; runtime.inputs = {"turn": {"text": "你好", "final": True}}
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"; runtime._identity_turns[identity] = "turn"
        await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert seen == {"json_mode": True, "text": "好的", "finished": True}
        assert any(kind == "speak.preview" and payload.get("text") == "好的" and payload.get("committed") is False
                   for kind, payload in events)
        assert any(kind == "speak.preview" and payload.get("status") == "committed"
                   for kind, payload in events)
        assert any(p.get("kind") == "SPEAK_ACCEPTED" for k, p in events if k == "agent.trace")
    asyncio.run(scenario())


def test_runtime_keeps_queued_speak_and_ignores_bad_actions():
    async def scenario():
        seen = {"feeds": [], "finishes": 0, "recoveries": []}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"先到'}
                    await asyncio.sleep(0)
                    yield {"type": "content", "text": '的回答"},"actions":[{"name":"bad"}]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "speech_partial"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finishes"] += 1

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            async def begin(self, *_args, **_kwargs):
                seen["queued_before_finish"] = True
                return Handle()

        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        identity = (runtime.episode_id, "request")
        runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"

        async def recovery(code, message, **kwargs):
            seen["recoveries"].append(code)
        runtime._queue_recovery = recovery

        await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert seen["queued_before_finish"] is True
        assert seen["feeds"] == ["先到", "的回答"]
        assert seen["finishes"] == 1
        assert seen["recoveries"] == []
        accepted = [
            payload for kind, payload in events
            if kind == "agent.trace" and payload.get("kind") == "LLM_OUTPUT_COMPAT_SPEAK_ACCEPTED"
        ]
        assert len(accepted) == 1
        assert accepted[0]["invalid_components"] == ["actions"]
        assert accepted[0]["actions_executed"] == 0
        assert accepted[0]["next_dispatch"] == "heartbeat"
        assert any(kind == "speak.preview" and payload.get("status") == "committed_speak_only"
                   for kind, payload in events)
    asyncio.run(scenario())


def test_runtime_keeps_queued_speak_on_truncated_envelope():
    async def scenario():
        seen = {"feeds": [], "finishes": 0, "recoveries": []}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": (
                        '{"speak":{"state":"speak","emotion":"calm","text":"保留这段语音"},'
                        '"actions":[]'
                    )}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "speech_truncated"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finishes"] += 1

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            async def begin(self, *_args, **_kwargs): return Handle()

        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        identity = (runtime.episode_id, "request")
        runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"

        async def recovery(code, message, **kwargs):
            seen["recoveries"].append(code)
        runtime._queue_recovery = recovery

        assert await runtime._step(identity, {"USER_INPUT_FINALIZED"}) == "compat_speak_only"
        assert seen["feeds"] == ["保留这段语音"]
        assert seen["finishes"] == 1
        assert seen["recoveries"] == []
        accepted = [
            payload for kind, payload in events
            if kind == "agent.trace" and payload.get("kind") == "LLM_OUTPUT_COMPAT_SPEAK_ACCEPTED"
        ]
        assert len(accepted) == 1
        assert accepted[0]["invalid_components"] == ["envelope_tail"]
        assert accepted[0]["actions_executed"] == 0

    asyncio.run(scenario())


def test_complete_playing_speak_replaces_directly_without_llm_recovery():
    async def scenario():
        seen = {"replace": [], "feeds": [], "recoveries": []}

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "new_speech"
            async def feed(self, text): seen["feeds"].append(text)
            async def finish(self): seen["finished"] = True

        class Output:
            active = True
            status = "SPEAKING"
            speak_snapshot = {"queue_status": "speaking", "speech_id": "old_speech"}
            async def replace_current(self, *_args, **kwargs):
                seen["replace"].append(kwargs)
                return Handle()
            def cancel_all(self, reason): seen.setdefault("cancel", []).append(reason)

        async def emit(*_): pass
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(None, Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        identity = (runtime.episode_id, "request")
        runtime._queue_recovery = lambda *args, **kwargs: seen["recoveries"].append(args[0])
        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", "新的完整播报"), ())
        await runtime._apply_decision_v06(identity, "turn", envelope)
        assert seen["replace"] == [{"emotion": "calm"}]
        assert seen["feeds"] == ["新的完整播报"] and seen["finished"]
        assert not seen["recoveries"]
        assert "cancel" not in seen
    asyncio.run(scenario())


def test_stream_prefix_replaces_playing_speak_and_only_feeds_new_handle():
    async def scenario():
        seen = {"replace": 0, "feeds": [], "recoveries": []}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","emotion":"calm","text":"新'}
                    await asyncio.sleep(0)
                    yield {"type": "content", "text": '内容"},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "new_speech"
            async def feed(self, text): seen["feeds"].append(text)
            async def finish(self): seen["finished"] = True

        class Output:
            active = True
            status = "SPEAKING"
            speak_snapshot = {"queue_status": "speaking", "speech_id": "old_speech"}
            async def replace_current(self, *_args, **_kwargs):
                seen["replace"] += 1
                return Handle()

        async def emit(*_): pass
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        identity = (runtime.episode_id, "request")
        runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"

        async def recovery(*args, **_kwargs): seen["recoveries"].append(args[0])
        runtime._queue_recovery = recovery
        await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert seen["replace"] == 1
        assert seen["feeds"] == ["新", "内容"] and seen["finished"]
        assert not seen["recoveries"]
    asyncio.run(scenario())


def test_streamed_prefix_divergence_replaces_with_complete_accumulated_text():
    async def scenario():
        seen = {"replace": 0, "feeds": []}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"已有改'}
                    yield {"type": "content", "text": '正"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "replacement"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): pass

        class Output:
            status = "SPEAKING"
            speak_snapshot = {"queue_status": "speaking", "speech_id": "old", "output_generation": 3,
                              "text": "已有播报"}
            async def replace_current(self, *_args, **_kwargs):
                seen["replace"] += 1
                return Handle()

        async def emit(*_args): pass
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        Output.speak_snapshot.update(conversation_id=runtime.episode_id, turn_id="turn")
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"
        await runtime._step(identity, {"USER_INPUT_REFINED"})
        assert seen["replace"] == 1 and seen["feeds"] == ["已有改", "正"]
    asyncio.run(scenario())


def test_model_speak_state_separates_knowledge_from_continuation_authority():
    snapshot = {
        "queue_status": "speaking", "conversation_id": "conversation",
        "turn_id": "old_turn", "text": "上一轮仍在播放", "speech_id": "speech",
    }
    previous = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="conversation", turn_id="new_turn",
    )
    assert previous == {
        "state": "speaking", "owner_scope": "previous_turn",
        "continuation_allowed": False, "observed_text": "上一轮仍在播放",
    }
    current = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="conversation", turn_id="old_turn",
    )
    assert current == {
        "state": "speaking", "owner_scope": "current_turn",
        "continuation_allowed": True, "target_text": "上一轮仍在播放",
    }
    assert AgentEpisodeRuntime._model_speak_state(
        {"queue_status": "empty"}, conversation_id="conversation", turn_id="new_turn",
    ) == {"state": "empty", "owner_scope": "none", "continuation_allowed": False}


def test_speak_snapshot_never_attaches_delivery_from_a_different_handle():
    output = object.__new__(EpisodeSpeechOutput)
    output._pending = []
    output.last_delivery = {"heard_text": "旧语音", "speech_id": "old_speech"}
    output.handle = SimpleNamespace(
        reason=None,
        task=SimpleNamespace(done=lambda: False),
        workspace=SimpleNamespace(playback_started=True, generation=2),
        state="READY",
        text="新语音",
        speech_id="new_speech",
        identity=("conversation", "request"),
        turn_id="new_turn",
        emotion_requested="calm",
    )

    snapshot = output.speak_snapshot

    assert snapshot["conversation_id"] == "conversation"
    assert snapshot["turn_id"] == "new_turn"
    assert snapshot["text"] == "新语音"
    assert "delivery" not in snapshot


def test_completed_prefix_guard_is_conversation_scoped_and_requires_full_delivery():
    output = SimpleNamespace(last_completed_speech={
        "speech_id": "old", "conversation_id": "other_conversation",
        "turn_id": "old_turn", "output_generation": 1,
        "heard_text": "已经播放", "delivery_status": "full", "status": "completed",
    })
    runtime = AgentEpisodeRuntime(
        None, SimpleNamespace(), output, lambda *_args: None,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )

    assert runtime._completed_speech_guard(("conversation", "request"), "new_turn") is None
    output.last_completed_speech["conversation_id"] = "conversation"
    output.last_completed_speech["delivery_status"] = "partial"
    assert runtime._completed_speech_guard(("conversation", "request"), "new_turn") is None
    output.last_completed_speech["delivery_status"] = "full"
    assert runtime._completed_speech_guard(("conversation", "request"), "new_turn")["text"] == "已经播放"


def test_active_previous_turn_prefix_is_replaced_with_suffix_not_appended():
    async def scenario():
        seen = {"feeds": [], "replace": 0, "append": 0}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"上一轮正在播放。'}
                    yield {"type": "content", "text": '这是当前轮的新回答。"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "replacement"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finished"] = True

        class Output:
            status = "SPEAKING"
            last_completed_speech = None
            speak_snapshot = None
            async def replace_current(self, *_args, **_kwargs):
                seen["replace"] += 1
                return Handle()
            async def append_successor(self, *_args, **_kwargs):
                seen["append"] += 1
                return Handle()

        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("new_turn")
        Output.speak_snapshot = {
            "queue_status": "speaking", "conversation_id": conversation.conversation_id,
            "turn_id": "old_turn", "speech_id": "old_speech", "output_generation": 1,
            "text": "上一轮正在播放。",
        }
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "new_turn"
        runtime.inputs = {"new_turn": {"text": "继续", "final": True}}
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "new_turn"

        await runtime._step(identity, {"USER_INPUT_FINALIZED"})

        assert seen["replace"] == 1 and seen["append"] == 0
        assert seen["feeds"] == ["这是当前轮的新回答。"] and seen["finished"]
        traces = [payload for kind, payload in events if kind == "agent.trace"]
        stripped = next(item for item in traces
                        if item["kind"] == "CROSS_TURN_SPEECH_PREFIX_STRIPPED")
        assert stripped["baseline_source"] == "active_previous_guard"

    asyncio.run(scenario())


def test_streamed_cross_turn_completed_prefix_is_stripped_before_tts():
    async def scenario():
        seen = {"feeds": [], "append": 0}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"能听到，你说吧，我在。'}
                    yield {"type": "content", "text": '我自己起名字的话，就叫小听吧。"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "new_speech"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finished"] = True

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            last_completed_speech = None
            async def begin(self, *_args, **_kwargs): return Handle()
            async def append_successor(self, *_args, **_kwargs):
                seen["append"] += 1
                return Handle()

        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("new_turn")
        output = Output()
        output.last_completed_speech = {
            "speech_id": "old_speech", "conversation_id": conversation.conversation_id,
            "turn_id": "old_turn", "output_generation": 1,
            "text": "能听到，你说吧，我在。", "heard_text": "能听到，你说吧，我在。",
            "delivery_status": "full", "termination": "completed", "status": "completed",
        }
        ledger = SessionLedger(); ledger.commit_input("old_turn", "能听到吗")
        ledger.commit_delivery(
            "old_speech", "能听到，你说吧，我在。",
            DeliveryReport(
                status="full", termination="completed", heard_text="能听到，你说吧，我在。",
            ),
        )
        runtime = AgentEpisodeRuntime(Provider(), Registry(), output, emit,
                                      conversation=conversation, ledger=ledger)
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "new_turn"
        runtime.inputs = {"new_turn": {"text": "给自己起个名字", "final": True}}
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "new_turn"

        await runtime._step(identity, {"USER_INPUT_FINALIZED"})

        assert seen["feeds"] == ["我自己起名字的话，就叫小听吧。"]
        assert seen["append"] == 0 and seen["finished"]
        traces = [payload for kind, payload in events if kind == "agent.trace"]
        assert any(item["kind"] == "CROSS_TURN_SPEECH_PREFIX_STRIPPED" for item in traces)
        context = runtime.messages({"USER_INPUT_FINALIZED"})
        assistant_history = [
            item["content"] for item in context if item["role"] == "assistant"
        ]
        assert assistant_history == [
            '<voice_delivery status="full">能听到，你说吧，我在。</voice_delivery>'
        ]
        packet = next(json.loads(item["content"]) for item in context
                      if item.get("context_kind") == "voice_request_context")
        runtime_tail = next(entry["content"] for entry in packet["entries"]
                            if entry["kind"] == "runtime_state")
        assert '"state":"empty"' in runtime_tail
        assert '"continuation_allowed":false' in runtime_tail
        assert "target_text" not in runtime_tail

    asyncio.run(scenario())


def test_streamed_same_turn_completed_prefix_uses_only_suffix_for_refinement():
    async def scenario():
        seen = {"feeds": [], "append": 0}

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"已经播出的回答。'}
                    yield {"type": "content", "text": '补充的新事实。"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "suffix_speech"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finished"] = True

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            last_completed_speech = None
            async def begin(self, *_args, **_kwargs): return Handle()
            async def append_successor(self, *_args, **_kwargs):
                seen["append"] += 1
                return Handle()

        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn-a")
        output = Output()
        output.last_completed_speech = {
            "speech_id": "completed_speech", "conversation_id": conversation.conversation_id,
            "turn_id": "turn-a", "output_generation": 1,
            "text": "已经播出的回答。", "heard_text": "已经播出的回答。",
            "delivery_status": "full", "termination": "completed", "status": "completed",
        }
        runtime = AgentEpisodeRuntime(Provider(), Registry(), output, emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "turn-a"
        runtime.inputs = {"turn-a": {"text": "原始转录", "final": True}}
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn-a"

        await runtime._step(identity, {"USER_INPUT_REFINED"})

        assert seen["feeds"] == ["补充的新事实。"]
        assert seen["append"] == 0 and seen["finished"]
        traces = [payload for kind, payload in events if kind == "agent.trace"]
        stripped = next(item for item in traces
                        if item["kind"] == "SPEECH_COMPLETED_SAME_TURN_PREFIX_STRIPPED")
        assert stripped["baseline_source"] == "completed_same_turn_guard"
        assert stripped["reason"] == "completed_same_turn_extension"

    asyncio.run(scenario())


def test_completed_same_turn_identical_speak_is_ignored_only_for_transcript_reassessment():
    async def scenario():
        begun, events = [], []

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            last_completed_speech = None
            async def begin(self, *_args, **_kwargs):
                begun.append(True)
                raise AssertionError("an identical same-turn replay must not reach TTS")

        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn-a")
        output = Output()
        output.last_completed_speech = {
            "speech_id": "completed_speech", "conversation_id": conversation.conversation_id,
            "turn_id": "turn-a", "output_generation": 1,
            "text": "已经播出的回答。", "heard_text": "已经播出的回答。",
            "delivery_status": "full", "termination": "completed", "status": "completed",
        }
        runtime = AgentEpisodeRuntime(None, Registry(), output, emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        identity = (runtime.episode_id, "request")
        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", "已经播出的回答。"), ())
        runtime._applying_transcript_reassessment = True

        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert not begun
        assert runtime.current_action == "wait"
        assert any(kind == "speech.append_ignored"
                   and payload["reason"] == "completed_same_turn_identical"
                   for kind, payload in events)

    asyncio.run(scenario())


def test_completed_same_turn_guard_does_not_extend_during_non_transcript_dispatch():
    class Registry:
        def begin_turn(self, _): pass
        def action_definitions(self): return []

    async def emit(*_args): pass
    conversation = ConversationRuntime(); conversation.begin_turn("turn-a")
    output = SimpleNamespace(
        last_completed_speech={
            "speech_id": "completed_speech", "conversation_id": conversation.conversation_id,
            "turn_id": "turn-a", "output_generation": 1,
            "text": "已经播出的回答。", "heard_text": "已经播出的回答。",
            "delivery_status": "full", "termination": "completed", "status": "completed",
        },
    )
    runtime = AgentEpisodeRuntime(None, Registry(), output, emit,
                                  conversation=conversation, ledger=SessionLedger())
    runtime.episode_id = conversation.conversation_id

    assert runtime._completed_speech_guard((runtime.episode_id, "request"), "turn-a") is None
    guard = runtime._completed_speech_guard(
        (runtime.episode_id, "request"), "turn-a", allow_same_turn=True,
    )
    assert guard is not None and guard["source"] == "completed_same_turn_guard"


def test_exact_previous_completed_text_remains_a_valid_explicit_replay():
    async def scenario():
        seen = []

        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"请再说一遍。"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Handle:
            speech_id = "replay"
            async def feed(self, value): seen.append(value)
            async def finish(self): pass

        class Output:
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
            last_completed_speech = None
            async def begin(self, *_args, **_kwargs): return Handle()

        async def emit(*_args): pass
        conversation = ConversationRuntime(); conversation.begin_turn("new_turn")
        output = Output()
        output.last_completed_speech = {
            "speech_id": "old", "conversation_id": conversation.conversation_id,
            "turn_id": "old_turn", "output_generation": 1, "text": "请再说一遍。",
            "heard_text": "请再说一遍。", "delivery_status": "full",
            "termination": "completed", "status": "completed",
        }
        runtime = AgentEpisodeRuntime(Provider(), Registry(), output, emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "new_turn"
        runtime.inputs = {"new_turn": {"text": "再说一遍", "final": True}}
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "new_turn"

        await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert seen == ["请再说一遍。"]

    asyncio.run(scenario())


def test_streamed_full_text_prefix_queues_only_suffix_without_replacement():
    async def scenario():
        seen = {"replace": 0, "suffixes": [], "feeds": []}
        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","text":"已有播报'}
                    yield {"type": "content", "text": '，新增句子"},"actions":[]}' }
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []
        class Handle:
            speech_id = "successor"
            async def feed(self, value): seen["feeds"].append(value)
            async def finish(self): seen["finished"] = True
        class Output:
            status = "SPEAKING"
            speak_snapshot = {"queue_status": "speaking", "speech_id": "old", "output_generation": 7,
                              "text": "已有播报"}
            async def replace_current(self, *_args, **_kwargs):
                seen["replace"] += 1; return Handle()
            async def append_successor(self, *_args, suffix, **_kwargs):
                seen["suffixes"].append(suffix); return Handle()
        events = []
        async def emit(kind, payload): events.append((kind, payload))
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(Provider(), Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id; runtime.last_turn_id = "turn"
        runtime.inputs = {"turn": {"text": "你好", "final": True}}
        Output.speak_snapshot.update(conversation_id=runtime.episode_id, turn_id="turn")
        identity = (runtime.episode_id, "request"); runtime.agent_step_id = "request"
        runtime._identity_turns[identity] = "turn"
        await runtime._step(identity, {"USER_INPUT_REFINED"})
        assert seen["replace"] == 0 and seen["suffixes"] == ["，新增句子"]
        assert seen["feeds"] == [] and seen["finished"]
        assert any(kind == "speech.append_waiting_prefix" for kind, _ in events)
    asyncio.run(scenario())


def test_speak_cancel_stops_playing_output_instead_of_ignoring_it():
    async def scenario():
        seen = []

        class Registry:
            def begin_turn(self, _): pass
            def action_definitions(self): return []

        class Output:
            active = True
            status = "SPEAKING"
            speak_snapshot = {"queue_status": "speaking", "speech_id": "old_speech"}
            def cancel_all(self, reason): seen.append(reason)

        async def emit(*_): pass
        conversation = ConversationRuntime(); conversation.begin_turn("turn")
        runtime = AgentEpisodeRuntime(None, Registry(), Output(), emit,
                                      conversation=conversation, ledger=SessionLedger())
        runtime.episode_id = conversation.conversation_id
        await runtime._apply_decision_v06((runtime.episode_id, "request"), "turn",
                                          DecisionEnvelope(SpeechDecision("cancel"), ()))
        assert seen == ["cancelled"]
        assert runtime.current_action == "wait"
    asyncio.run(scenario())


def test_replacement_starts_before_slow_old_cleanup_finishes():
    async def scenario():
        starts, old_cleanup = [], asyncio.Event()

        async def emit(*_args): pass
        runtime = SimpleNamespace(gate=lambda _: None, emit=emit, user_speaking=False, closed=False)
        player = SimpleNamespace(stop_and_snapshot=lambda _key: {"started": True})
        controller = SimpleNamespace(
            tts=None, player=player, options=SimpleNamespace(), bridge_options=SimpleNamespace(),
            _turn_timings={}, _track=lambda task: task,
        )
        output = EpisodeSpeechOutput(controller); output.runtime = runtime

        async def slow_run(handle):
            starts.append(handle.speech_id)
            try:
                async for _ in handle.stream():
                    pass
            except asyncio.CancelledError:
                await asyncio.sleep(.20)  # provider cancel / socket close analogue
                old_cleanup.set()

        output._run = slow_run
        old = await output.begin(("episode", "request"), "turn")
        await asyncio.sleep(0)
        old.workspace.playback_started = True
        new = await output.replace_current(("episode", "request"), "turn")
        await asyncio.sleep(.03)
        assert new.speech_id in starts
        assert not old_cleanup.is_set()
        # The old provider reader can still be in its slow cleanup, but any
        # PCM arriving from it is rejected by the generation/token gate.
        assert not output._valid(old.workspace.token)
        assert output._valid(new.workspace.token)
        await new.finish()
        await old_cleanup.wait()
        await new.task
    asyncio.run(scenario())


def test_barge_cancel_tolerates_legacy_single_field_identity():
    """A diagnostic correlation gap must not become input_frame_failed."""
    async def scenario():
        async def emit(*_args): pass
        runtime = SimpleNamespace(gate=lambda _: None, emit=emit, user_speaking=False, closed=False)
        player = SimpleNamespace(stop_and_snapshot=lambda _key: {"started": True})
        controller = SimpleNamespace(
            tts=None, player=player, options=SimpleNamespace(), bridge_options=SimpleNamespace(),
            _turn_timings={}, _track=lambda task: task,
        )
        output = EpisodeSpeechOutput(controller); output.runtime = runtime
        blocked = asyncio.Event()

        async def fake_run(_handle):
            await blocked.wait()

        output._run = fake_run
        handle = await output.begin(("legacy_conversation_only",), "turn")
        handle.workspace.playback_started = True
        output.cancel_all("interrupted")
        await asyncio.sleep(0)
        assert handle.identity == ("legacy_conversation_only", None)
        assert handle.reason == "interrupted"
        assert handle.task.done() and handle.task.cancelled()
    asyncio.run(scenario())
