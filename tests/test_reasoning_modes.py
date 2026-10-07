"""运行模式退役验收：仅快速和深度，历史均衡能力不能重新进入运行链路。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from voice_terminal.agent.decision_application import ProtocolError
from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.config import AppConfig, LlmConfig, load_config
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.server.product import sanitize_runtime_snapshot
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


class _Registry:
    """Registry（动作目录）：主代理只暴露固定的影子唤醒动作。"""

    def action_definitions(self):
        return [{
            "name": "request_shadow",  # name（动作名称）：唤醒影子代理。
            "parameters": {  # parameters（参数结构）：只发送固定唤醒信号。
                "type": "object",
                "properties": {"request": {"type": "boolean", "const": True}},
                "required": ["request"],
                "additionalProperties": False,
            },
        }]

    def service_status(self, _name):
        return "READY"


def _episode(provider=None, **options):
    """episode（代理轮次）：创建不连接音频与外部模型的真实运行时。"""
    conversation = ConversationRuntime()
    conversation.begin_turn("turn-a")
    runtime = AgentEpisodeRuntime(
        provider, _Registry(),
        SimpleNamespace(status="IDLE", active=False, speak_snapshot={"queue_status": "empty"}),
        AsyncMock(), conversation=conversation, ledger=SessionLedger(), **options,
    )
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "你好", "final": True}
    return runtime


def test_config_runtime_and_public_snapshot_default_to_fast(tmp_path):
    default_yaml = Path(__file__).resolve().parents[1] / "config/default.yaml"
    assert load_config(default_yaml).llm.reasoning_mode == "fast"
    assert AppConfig.from_mapping({}).llm.reasoning_mode == "fast"
    assert _episode().reasoning_mode == "fast"
    session = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
    assert session.snapshot().payload["reasoning_mode"] == "fast"
    assert sanitize_runtime_snapshot({})["reasoning_mode"] == "fast"


@pytest.mark.parametrize("mode", ["fast", "deep"])
def test_config_accepts_only_current_modes(mode):
    assert LlmConfig(reasoning_mode=mode).reasoning_mode == mode
    assert AppConfig.from_mapping({"llm": {"reasoning_mode": mode}}).llm.reasoning_mode == mode


@pytest.mark.parametrize("mode", ["balanced", "invalid", ""])
def test_retired_and_invalid_modes_are_rejected_at_config_and_agent_boundaries(mode):
    with pytest.raises(ValueError, match="运行模式"):
        LlmConfig(reasoning_mode=mode)
    with pytest.raises(ValueError, match="运行模式"):
        _episode(reasoning_mode=mode)
    controller = object.__new__(EpisodeController)
    with pytest.raises(ValueError, match="运行模式"):
        controller.set_reasoning_mode(mode)
    assert not hasattr(controller, "reasoning_mode")


def test_legacy_thinking_switch_cannot_enable_an_archived_action():
    config = AppConfig.from_mapping({"llm": {"thinking_action_enabled": True}})
    assert config.llm.reasoning_mode == "fast"
    assert not hasattr(config.llm, "thinking_action_enabled")
    with pytest.raises(ValueError, match="已归档"):
        decision_schema([{"name": "request_thinking", "parameters": {}}])


@pytest.mark.parametrize("mode", ["fast", "deep"])
@pytest.mark.parametrize("tools_enabled", [True, False])
def test_current_modes_expose_no_thinking_action_or_continuation_state(mode, tools_enabled):
    runtime = _episode(reasoning_mode=mode, tools_enabled=tools_enabled)
    assert "request_thinking" not in json.dumps(runtime._decision_schema())
    assert "request_thinking" not in runtime._tool_availability()
    assert "request_thinking" not in json.dumps(runtime.messages({"USER_INPUT_FINALIZED"}), ensure_ascii=False)
    assert "thinking_lease_turn" not in runtime.snapshot()


@pytest.mark.parametrize("mode, expected_effort", [("fast", "none"), ("deep", "low")])
def test_real_agent_dispatch_uses_the_selected_effort_once(mode, expected_effort):
    async def scenario():
        class Provider:
            """Provider（模型适配器替身）：记录真实派发参数，返回合法等待决策。"""

            transport = "responses"  # transport（传输协议）：响应接口。
            supports_decision_schema = True  # supports_decision_schema（支持决策结构约束）。

            def __init__(self):
                self.requests = []  # requests（请求列表）：保留本地测试参数。

            def stream_events(self, _messages, **kwargs):
                self.requests.append(kwargs)

                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'}
                    yield {"type": "finish", "reason": "completed"}

                return stream()

        provider = Provider()
        runtime = _episode(provider, reasoning_mode=mode)
        assert await runtime._start_dispatch({"USER_INPUT_FINALIZED"}) is True
        assert await runtime.active_request == "completed"
        assert len(provider.requests) == 1
        assert provider.requests[0]["reasoning_effort"] == expected_effort
        assert not runtime._background_tools
        assert not runtime.recovery_queue

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["fast", "deep"])
def test_retired_action_is_rejected_before_any_shadow_wake(mode):
    async def scenario():
        runtime = _episode(reasoning_mode=mode)
        runtime.shadow_waker = Mock()
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "让我确认一下。"),
            (Action("request_thinking", {"request": True}), Action("request_shadow", {"request": True})),
        )
        with pytest.raises(ProtocolError, match="unsupported action"):
            await runtime._dispatch_actions_v06((runtime.episode_id, "request-a"), "turn-a", envelope)
        runtime.shadow_waker.assert_not_called()
        assert not runtime._background_tools

    asyncio.run(scenario())


def test_session_mode_command_publishes_valid_modes_and_preserves_state_on_rejection(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
        runtime.controller = SimpleNamespace(set_reasoning_mode=Mock())
        runtime.bus = SimpleNamespace(publish=AsyncMock())
        for mode in ("deep", "fast"):
            assert await runtime.set_reasoning_mode(mode) is True
            runtime.controller.set_reasoning_mode.assert_called_with(mode)
            assert runtime.bus.publish.call_args.args[0].payload["reasoning_mode"] == mode
        for mode in ("balanced", "invalid"):
            assert await runtime.set_reasoning_mode(mode) is False
            assert runtime.reasoning_mode == "fast"
        assert runtime.controller.set_reasoning_mode.call_count == 2
        assert runtime.bus.publish.await_count == 2

    asyncio.run(scenario())
