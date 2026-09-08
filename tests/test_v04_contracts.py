from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import AgentDecisionScheduler, AgentEpisodeRuntime, control_schemas
from voice_terminal.agent.speech_stream import SpeechArgumentStream
from voice_terminal.agent.tool_calls import ToolCallAssembler
from voice_terminal.config import load_config
from voice_terminal.config import AppConfig
from voice_terminal.core.events import event
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.server.snapshot import ProtocolSequencer
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime, EpochReason


def test_low_thinking_payload_is_explicit() -> None:
    client = DeepSeekClient(base_url="https://example.invalid", model="deepseek-v4-flash",
                            api_key="key", thinking="enabled", reasoning_effort="low")
    payload = client._request_payload([{"role": "user", "content": "你好"}])
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"


def test_thinking_tools_omit_incompatible_tool_choice() -> None:
    client = DeepSeekClient(base_url="https://example.invalid", model="deepseek-v4-flash",
                            api_key="key", thinking="enabled", reasoning_effort="low")
    payload = client._request_payload([{"role": "user", "content": "你好"}],
                                      tools=control_schemas(), tool_choice="required")
    assert payload["thinking"] == {"type": "enabled"}
    assert "tool_choice" not in payload


def test_prefill_controls_and_response_check_are_agent_events() -> None:
    names = {item["function"]["name"] for item in control_schemas()}
    assert {"set_prefill", "clear_prefill"} <= names
    assert AgentDecisionScheduler.urgent >= {"RESPONSE_CHECK", "USER_INPUT_FINALIZED"}


def test_v04_debug_events_are_public_protocol_events() -> None:
    sequencer = ProtocolSequencer("session_a")
    for name in ("agent.prefill", "agent.reasoning", "speech.queued"):
        assert sequencer.public(event(name, "input_a"))


def test_v04_defaults_enable_prefill_response_check_and_low_reasoning() -> None:
    config = load_config(Path("config/default.yaml"))
    assert config.llm.thinking == "enabled"
    assert config.llm.reasoning_effort == "low"
    assert config.agent.response_check_ms == 1500
    assert config.speech.prefill_enabled is True
    assert config.tts.session_reuse is True


def test_removed_prediction_configuration_fails_fast() -> None:
    with pytest.raises(TypeError, match="predictions"):
        AppConfig.from_mapping({"smart_turn": {"predictions": True}})


def test_tool_delta_assembly_remains_available_to_the_v04_agent() -> None:
    assembler = ToolCallAssembler()
    assembler.feed({"index": 0, "id": "call_", "function": {"name": "web_search", "arguments": '{"query":"'}})
    assembler.feed({"index": 0, "id": "a", "function": {"arguments": 'latest"}'}})
    assert assembler.complete() == [{"id": "call_a", "type": "function",
                                    "function": {"name": "web_search", "arguments": '{"query":"latest"}'}}]


def test_speak_schema_and_incremental_parser_support_atomic_replacement() -> None:
    speak = next(item["function"] for item in control_schemas() if item["function"]["name"] == "speak")
    assert speak["parameters"]["properties"]["replace_current"]["type"] == "boolean"
    assert "replace_current" not in speak["parameters"]["required"]

    parser = SpeechArgumentStream()
    assert parser.feed('{"emotion":"calm","replace_current":true,"text":"修正') == "修正"
    assert parser.replace_current is True
    assert parser.feed('后的完整内容"}') == "后的完整内容"
    assert parser.finish() == "修正后的完整内容"

    invalid_order = SpeechArgumentStream()
    invalid_order.feed('{"emotion":"calm","text":"先发出的前缀"')
    with pytest.raises(ValueError, match="replace_current must precede"):
        invalid_order.feed(',"replace_current":true}')


def test_refinement_is_supplemental_context_and_only_semantic_changes_wake_agent() -> None:
    class Registry:
        def begin_turn(self, _conversation_id):
            return None

    async def emit(_kind, _payload):
        return None

    conversation = ConversationRuntime()
    runtime = AgentEpisodeRuntime(
        provider=None, registry=Registry(), output=SimpleNamespace(active=False, status="IDLE"),
        emit=emit, conversation=conversation, ledger=SessionLedger(),
    )
    wakes = []
    runtime._wake = lambda kind: wakes.append(kind)

    asyncio.run(runtime.observe("USER_INPUT_FINALIZED", turn_id="turn_a", text="明天开会"))
    epoch_after_final = conversation.epoch
    asyncio.run(runtime.observe("USER_INPUT_REFINED", turn_id="turn_a", text="明天开会"))
    assert conversation.epoch == epoch_after_final
    assert wakes == ["USER_INPUT_FINALIZED"]

    asyncio.run(runtime.observe("USER_INPUT_REFINED", turn_id="turn_a", text="明天下午开会"))
    assert conversation.epoch == epoch_after_final
    assert wakes[-1] == "USER_INPUT_REFINED"
    projected = runtime.messages({"USER_INPUT_REFINED"})
    user_messages = [item for item in projected if item.get("role") == "user"]
    assert user_messages[-1]["content"] == "明天开会"
    assert any("明天下午开会" in item.get("content", "") for item in projected if item.get("role") == "system")


def test_agent_observation_synchronizes_controller_epoch_without_advancing_it() -> None:
    class Registry:
        def begin_turn(self, _conversation_id):
            return None

    async def emit(_kind, _payload):
        return None

    conversation = ConversationRuntime()
    conversation.begin_turn("turn_a")
    runtime = AgentEpisodeRuntime(
        provider=None, registry=Registry(), output=SimpleNamespace(active=False, status="IDLE", invalidate=lambda _reason: None),
        emit=emit, conversation=conversation, ledger=SessionLedger(),
    )
    runtime._wake = lambda _kind: None

    asyncio.run(runtime.observe("USER_SEGMENT_UPDATED", turn_id="turn_a", text="先说一半"))
    assert conversation.epoch == 0

    conversation.advance_epoch(EpochReason.INPUT_FINALIZED)
    asyncio.run(runtime.observe("USER_INPUT_FINALIZED", turn_id="turn_a", text="完整输入"))
    assert conversation.epoch == 1
    assert runtime.context_version == 1


def test_agent_requires_a_control_tool_and_discards_ordinary_content() -> None:
    async def scenario() -> None:
        calls = []
        events = []

        class Provider:
            def stream_events(self, _messages, *, tools):
                calls.append(tools)

                async def stream():
                    yield {"type": "content", "text": "这段普通 content 不应发声"}
                    yield {"type": "tool_delta", "call": {
                        "index": 0, "id": "call_wait",
                        "function": {"name": "wait_for_input", "arguments": "{}"},
                    }}
                    yield {"type": "finish", "reason": "tool_calls"}

                return stream()

        class Registry:
            def begin_turn(self, _conversation_id):
                return None

            def schemas(self):
                return []

        async def emit(kind, payload):
            events.append((kind, payload))

        conversation = ConversationRuntime()
        conversation.begin_turn("turn_a")
        runtime = AgentEpisodeRuntime(
            provider=Provider(), registry=Registry(), output=SimpleNamespace(active=False, status="IDLE"),
            emit=emit, conversation=conversation, ledger=SessionLedger(),
        )
        runtime.episode_id = conversation.conversation_id
        runtime.last_turn_id = "turn_a"
        runtime.inputs = {"turn_a": {"text": "已定稿输入", "final": True}}
        runtime.user_speaking = False
        identity = (runtime.episode_id, "step_a", conversation.epoch)
        runtime.agent_step_id = identity[1]
        runtime._identity_turns[identity] = "turn_a"

        await runtime._step(identity, {"USER_INPUT_FINALIZED"})

        assert calls and calls[0]
        assert any(item[0] == "agent.trace" and item[1]["kind"] == "ASSISTANT_CONTENT_DISCARDED" for item in events)
        assert not any(item[0] == "speech.text_delta" for item in events)

    asyncio.run(scenario())


def test_session_ledger_is_the_only_context_and_input_history_store() -> None:
    ledger = SessionLedger(limit=2)
    ledger.commit_input("input_a", "查询天气")
    ledger.commit_delivery("speech_a", "我来查看。", DeliveryReport(
        status="full", termination="completed", heard_text="我来查看。"))
    record = ledger.append_record({"turn_id": "turn_a", "transcript": "查询天气", "conversation_id": "conversation_a"})
    assert record.turn_id == "turn_a"
    assert ledger.context_messages("")[-2:] == [
        {"role": "user", "content": "查询天气"},
        {"role": "assistant", "content": "我来查看。"},
    ]


def test_speech_queue_serializes_prefill_and_formal_without_busy_rejection() -> None:
    async def scenario() -> None:
        events, played = [], []

        async def emit(kind, payload):
            events.append((kind, payload))

        runtime = SimpleNamespace(gate=lambda _identity: None, emit=emit)
        controller = SimpleNamespace(tts=None, player=None, options=SimpleNamespace(), bridge_options=SimpleNamespace())
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime

        async def fake_run(handle):
            text = ""
            async for delta in handle.stream():
                text += delta
            played.append((handle.kind, text))

        output._run = fake_run
        identity = ("episode", "decision", 1)
        prefill = await output.begin(identity, "input", kind="prefill")
        formal = await output.begin(identity, "input", kind="formal")
        await prefill.feed("好，我查一下。")
        await prefill.finish()
        await formal.feed("这是正式回答。")
        await formal.finish()
        await asyncio.gather(prefill.task, formal.task)
        assert played == [("prefill", "好，我查一下。"), ("formal", "这是正式回答。")]
        assert [kind for kind, _payload in events].count("speech.queued") == 2

    asyncio.run(scenario())


def test_targeted_prefill_cancellation_does_not_cancel_later_formal_speech() -> None:
    async def scenario() -> None:
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        runtime = SimpleNamespace(gate=lambda _identity: None, emit=emit)
        controller = SimpleNamespace(tts=None, player=None, options=SimpleNamespace(), bridge_options=SimpleNamespace())
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime

        blocker = asyncio.create_task(asyncio.Event().wait())
        output._worker = blocker  # Keep both items queued for a deterministic scope check.
        identity = ("episode", "decision", 1)
        prefill = await output.enqueue_text(identity, "input", "我看一下。", kind="prefill")
        formal = await output.enqueue_text(identity, "input", "正式回答。", kind="formal")
        output.cancel(prefill.speech_id, "cleared")
        await asyncio.gather(prefill.task, return_exceptions=True)
        assert prefill.reason == "cleared"
        assert formal.reason is None
        assert not formal.task.cancelled()
        output.cancel(formal.speech_id, "stopped")
        blocker.cancel()
        await asyncio.gather(formal.task, blocker, return_exceptions=True)

    asyncio.run(scenario())


def test_replace_current_stops_old_output_before_queueing_new_output() -> None:
    async def scenario() -> None:
        events = []

        async def emit(kind, payload):
            events.append((kind, payload))

        runtime = SimpleNamespace(gate=lambda _identity: None, emit=emit)
        controller = SimpleNamespace(
            tts=None, player=None, options=SimpleNamespace(), bridge_options=SimpleNamespace(),
            _turn_timings={"input": {}},
        )
        output = EpisodeSpeechOutput(controller)
        output.runtime = runtime

        played = []

        async def fake_run(handle):
            text = ""
            try:
                async for delta in handle.stream():
                    text += delta
            except asyncio.CancelledError:
                raise
            played.append((handle.speech_id, text))

        output._run = fake_run
        identity = ("episode", "decision", 1)
        old = await output.begin(identity, "input", kind="formal")
        await asyncio.sleep(0)
        await old.feed("旧回答")
        replacement = await output.replace_current(identity, "input", emotion="calm")
        assert old.reason == "replaced"
        assert controller._turn_timings["input"]["replacement_count"] == 1
        await replacement.feed("新回答")
        await replacement.finish()
        await asyncio.gather(old.task, replacement.task, return_exceptions=True)

        assert played == [(replacement.speech_id, "新回答")]
        replaced = [payload for kind, payload in events if kind == "speech.replaced"]
        assert replaced and replaced[0]["speech_id"] == old.speech_id

    asyncio.run(scenario())
