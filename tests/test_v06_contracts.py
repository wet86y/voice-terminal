from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.agent.decision_envelope import EnvelopeError, SpeechDecisionEnvelopeStream
from voice_terminal.agent.episode import AgentDecisionScheduler, AgentEpisodeRuntime
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.sentences import SentenceStore, map_refinement
from voice_terminal.config import AppConfig, load_config
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.core.events import event
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.server.snapshot import ProtocolSequencer
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
    assert AgentDecisionScheduler(AgentOptions()).accepts("TOOL_COMPLETED")
    with pytest.raises(TypeError, match="predictions"):
        AppConfig.from_mapping({"smart_turn": {"predictions": True}})


def test_debug_events_are_public_protocol_events():
    sequencer = ProtocolSequencer("session_a")
    assert sequencer.public(event("agent.reasoning", "input_a"))
    assert sequencer.public(event("speak.preview", "input_a"))
    assert sequencer.public(event("speech.queued", "input_a"))


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
