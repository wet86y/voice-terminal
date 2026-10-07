"""均衡模式历史测试：不参与默认测试，仅与归档前实现配套。"""


from __future__ import annotations


import asyncio


import json


from types import SimpleNamespace


import pytest


from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision


from voice_terminal.agent.context_audit import build_context_snapshot


from voice_terminal.agent.decision_envelope import (
    EnvelopeError, SpeechDecisionEnvelopeStream, envelope_error_diagnostics,
    json_structure_fingerprint, parse_compatible_decision_envelope,
    provider_schema_diagnostics,
)


from voice_terminal.agent.decision_schema import THINKING_ACTION, decision_schema


from voice_terminal.agent.episode import AgentEpisodeRuntime, PROMPT


from voice_terminal.providers.deepseek import DeepSeekClient, DeepSeekHttpError, DeepSeekTransportError


from voice_terminal.providers.deepseek import DeepSeekError


from voice_terminal.providers.deepseek import DeepSeekResponseTerminalError


from voice_terminal.session.ledger import SessionLedger


from voice_terminal.session.context_frames import ContextFrame, ContextLane


from voice_terminal.session.runtime import ConversationRuntime


from voice_terminal.shadow_agent.contracts import ShadowEvent


def test_decision_schema_keeps_wire_shape_and_reserves_thinking_action():
    schema = decision_schema([{
        "name": "web_search",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }], allow_thinking=True)
    assert schema["required"] == ["speak", "actions"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["actions"]["items"]
    assert item["required"] == ["name", "arguments"]
    assert item["properties"]["name"]["enum"] == ["web_search", THINKING_ACTION]
    assert item["properties"]["arguments"]["properties"]["request"] == {"type": "boolean", "const": True}


class _Registry:
    def begin_turn(self, _turn):
        pass

    def cancel_turn(self, _turn):
        pass

    def action_definitions(self):
        return []


class _Output:
    status = "IDLE"
    active = False
    speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

    def cancel_all(self, _reason):
        pass


def _runtime(provider):
    async def emit(*_args):
        pass

    conversation = ConversationRuntime()
    conversation.begin_turn("turn-a")
    runtime = AgentEpisodeRuntime(
        provider, _Registry(), _Output(), emit,
        conversation=conversation, ledger=SessionLedger(),
    )
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    return runtime


@pytest.mark.skip(reason="voice business tools were replaced by the content-free Shadow wake")
def test_thinking_and_business_actions_release_one_barrier_after_tools():
    async def scenario():
        provider = SimpleNamespace(thinking_action_enabled=True)
        runtime = _runtime(provider)
        released = asyncio.Event()
        wakes = []

        async def execute(_identity, _turn_id, actions, *, wake=True):
            assert [action.name for action in actions] == ["web_search"]
            assert wake is True
            await released.wait()
            return True

        runtime._execute_actions_v06 = execute
        runtime._wake = wakes.append
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "让我想一想。"),
            (
                Action(THINKING_ACTION, {"request": True}),
                Action("web_search", {"query": "current weather"}),
            ),
        )
        await runtime._dispatch_actions_v06((runtime.episode_id, "request"), "turn-a", envelope)
        await asyncio.sleep(0)
        assert wakes == [] and runtime._thinking_lease_turn is None
        released.set()
        await asyncio.gather(*tuple(runtime._background_tools))
        assert wakes == ["THINKING_READY"]
        assert runtime._thinking_lease_turn == "turn-a"
        assert runtime._thinking_prefix_by_turn["turn-a"] == "让我想一想。"

    asyncio.run(scenario())


def test_secondary_mailbox_actions_and_thinking_follow_normal_dispatch_rules():
    async def scenario():
        runtime = _runtime(SimpleNamespace(thinking_action_enabled=True))
        shadow_wakes = []
        runtime.shadow_waker = lambda: shadow_wakes.append("wake") or "started"
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "有必要的新进展。"),
            (Action(THINKING_ACTION, {"request": True}), Action("request_shadow", {"request": True})),
        )
        await runtime._dispatch_actions_v06(
            (runtime.episode_id, "request-a"), "turn-a", envelope,
            system_check=True, system_check_id="system-check-a",
            system_check_prompt_kind="secondary_mailbox",
        )
        await asyncio.gather(*tuple(runtime._background_tools))
        assert shadow_wakes == ["wake"]
        assert any(item["kind"] == "THINKING_REQUESTED" for item in runtime.trace)
        assert not any(item["kind"] == "SYSTEM_CHECK_ACTION_REJECTED" for item in runtime.trace)

    asyncio.run(scenario())


def test_final_silence_actions_and_thinking_are_rejected_before_execution():
    async def scenario():
        runtime = _runtime(SimpleNamespace(thinking_action_enabled=True))
        runtime._system_check_turn_ids.add("system-check-a")
        shadow_wakes = []
        runtime.shadow_waker = lambda: shadow_wakes.append("wake") or "started"
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "系统检查播报。"),
            (
                Action(THINKING_ACTION, {"request": True}),
                Action("request_shadow", {"request": True}),
            ),
        )
        await runtime._dispatch_actions_v06(
            (runtime.episode_id, "request-a"), "system-check-a", envelope,
            system_check=True,
            system_check_id="system-check-a",
            system_check_prompt_kind="final_silence",
        )

        assert shadow_wakes == []
        assert runtime._background_tools == set()
        rejected = [item for item in runtime.trace if item["kind"] == "SYSTEM_CHECK_ACTION_REJECTED"]
        assert rejected and rejected[0]["action_names"] == [THINKING_ACTION, "request_shadow"]
        assert rejected[0]["prompt_kind"] == "final_silence"

    asyncio.run(scenario())


def test_thinking_action_is_explicitly_present_in_tool_availability():
    balanced = _runtime(SimpleNamespace(thinking_action_enabled=True))
    balanced.reasoning_mode = "balanced"
    balanced._frozen_thinking_action_enabled = True
    assert balanced._tool_availability()[THINKING_ACTION] == "ready"

    fast = _runtime(SimpleNamespace(thinking_action_enabled=False))
    fast.reasoning_mode = "fast"
    fast._frozen_thinking_action_enabled = False
    assert THINKING_ACTION not in fast._tool_availability()


def test_new_user_turn_invalidates_pending_thinking_barrier():
    async def scenario():
        provider = SimpleNamespace(thinking_action_enabled=True)
        runtime = _runtime(provider)
        runtime._wake = lambda _kind: None
        runtime._thinking_lease_turn = "turn-a"
        runtime._thinking_prefix_by_turn["turn-a"] = "让我想想。"
        generation = runtime._thinking_barrier_generation
        await runtime.observe("USER_SEGMENT_UPDATED", turn_id="turn-b", text="new input")
        assert runtime.last_turn_id == "turn-b"
        assert runtime._thinking_lease_turn is None
        assert "turn-a" not in runtime._thinking_prefix_by_turn
        assert runtime._thinking_barrier_generation == generation + 1

    asyncio.run(scenario())
