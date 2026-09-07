from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import AgentDecisionScheduler, control_schemas
from voice_terminal.agent.tool_calls import ToolCallAssembler
from voice_terminal.config import load_config
from voice_terminal.config import AppConfig
from voice_terminal.core.events import event
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.server.snapshot import ProtocolSequencer
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger


def test_low_thinking_payload_is_explicit() -> None:
    client = DeepSeekClient(base_url="https://example.invalid", model="deepseek-v4-flash",
                            api_key="key", thinking="enabled", reasoning_effort="low")
    payload = client._request_payload([{"role": "user", "content": "你好"}])
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"


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
    assert config.agent.response_check_ms == 6000
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


def test_session_ledger_is_the_only_context_and_input_history_store() -> None:
    ledger = SessionLedger(limit=2)
    ledger.commit_input("input_a", "查询天气")
    ledger.commit_delivery("speech_a", "我来查看。", DeliveryReport(
        status="full", termination="completed", heard_text="我来查看。"))
    record = ledger.append_record({"input_id": "input_a", "transcript": "查询天气", "episode_id": "episode_a"})
    assert record.input_id == "input_a"
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
