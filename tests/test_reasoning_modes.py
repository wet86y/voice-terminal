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
from voice_terminal.agent.prompts import DEEP_THINKING_PROMPT
from voice_terminal.config import AppConfig, LlmConfig, load_config
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.providers.deepseek import DeepSeekClient
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


def _episode(provider=None, *, conversation=None, ledger=None, **options):
    """episode（代理轮次）：可复用 conversation（对话状态）和 ledger（会话账本）。"""
    conversation = conversation if conversation is not None else ConversationRuntime()
    conversation.begin_turn("turn-a")
    runtime = AgentEpisodeRuntime(
        provider, _Registry(),
        SimpleNamespace(status="IDLE", active=False, speak_snapshot={"queue_status": "empty"}),
        AsyncMock(), conversation=conversation,
        ledger=ledger if ledger is not None else SessionLedger(), **options,
    )
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "你好", "final": True}
    return runtime


def test_config_runtime_and_public_snapshot_default_to_deep(tmp_path):
    default_yaml = Path(__file__).resolve().parents[1] / "config/default.yaml"
    assert load_config(default_yaml).llm.reasoning_mode == "deep"
    assert AppConfig.from_mapping({}).llm.reasoning_mode == "deep"
    assert _episode().reasoning_mode == "deep"
    session = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
    assert session.snapshot().payload["reasoning_mode"] == "deep"
    assert sanitize_runtime_snapshot({})["reasoning_mode"] == "deep"


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
    assert config.llm.reasoning_mode == "deep"
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


@pytest.mark.parametrize("initial_mode", ["fast", "deep"])
@pytest.mark.parametrize("request_state", ["dispatched", "streaming", "completed"])
def test_pause_switch_resume_updates_provider_effort_without_replacing_live_work(
    tmp_path, monkeypatch, initial_mode, request_state,
):
    """暂停后双向切换：Responses（响应接口）参数更新，原请求与会话历史保留。"""
    async def scenario():
        session = SessionRuntime(
            AppConfig.from_mapping({"llm": {"reasoning_mode": initial_mode}}),
            runtime_data_dir=tmp_path / "runtime",
        )
        session._application_phased_startup = False
        session.state.wake_always_on = False
        session.bus = SimpleNamespace(publish=AsyncMock())
        session._apply_pending_product_settings = AsyncMock(return_value=True)
        session._deliver_voice_mailbox = AsyncMock()
        monkeypatch.setattr(session.continuity, "user_started", Mock())

        provider = DeepSeekClient(
            base_url="https://example.test", model="voice-model", api_key="in-memory-only",
            transport="responses", thinking="disabled",
        )
        requests = []  # requests（实际接口请求体）：通过真实适配器归一化与构造。
        started = asyncio.Event()
        release = asyncio.Event()

        def assert_request_mode(payload, mode):
            assert payload["reasoning"]["effort"] == ("low" if mode == "deep" else "none")
            assert payload["instructions"] == requests[0]["instructions"]
            assert DEEP_THINKING_PROMPT not in payload["instructions"]
            mode_prompts = [frame["content"][0]["text"] for frame in payload["input"]
                            if frame["content"][0]["text"] == DEEP_THINKING_PROMPT]
            assert mode_prompts == ([DEEP_THINKING_PROMPT] if mode == "deep" else [])

        async def stream_responses(messages, *, schema, schema_name, reasoning_effort, **_kwargs):
            requests.append(provider._responses_payload(
                messages, model=provider.model, schema=schema, schema_name=schema_name,
                reasoning_effort=reasoning_effort, max_output_tokens=provider.max_output_tokens,
            ))
            if len(requests) == 1:
                started.set()
                await release.wait()
            yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'}
            yield {"type": "finish", "reason": "completed"}

        monkeypatch.setattr(provider, "_stream_responses", stream_responses)
        # 仅替代硬件启停与网络传输；会话命令、控制器、派发与接口请求体使用真实实现。
        async def base_start(controller):
            controller.state.listening = True

        async def base_stop(controller):
            controller.state.listening = False

        monkeypatch.setattr(RealtimeController, "start_listening", base_start)
        monkeypatch.setattr(RealtimeController, "stop_listening", base_stop)
        monkeypatch.setattr(provider, "prewarm", AsyncMock())
        episode = _episode(
            provider, reasoning_mode=initial_mode,
            conversation=session.conversation, ledger=session.history,
        )
        controller = object.__new__(EpisodeController)
        controller.state = session.state
        controller.workspace = None
        controller.llm = provider
        controller.output = episode.output
        controller.output.release_held = Mock()
        controller._cancel_early_silence = AsyncMock()
        controller._cancel_tts_prewarm = AsyncMock()
        controller.episode = episode
        controller.reasoning_mode = initial_mode
        controller.wake_always_on = False  # 本用例验证模式切换，关闭静默回到唤醒的流程。
        session.controller = controller
        identity = episode.episode_id
        session.history.commit_input("turn-a", "你好")
        first_request = None
        try:
            await session.start_listening()
            assert await episode._start_dispatch({"USER_INPUT_FINALIZED"}) is True
            first_request = episode.active_request
            # request_state（首请求阶段）：待构造、响应流中或已完成，分别覆盖切换边界。
            if request_state != "dispatched":
                await asyncio.wait_for(started.wait(), timeout=2)
            if request_state == "completed":
                release.set()
                assert await first_request == "completed"
                await episode._finish_active_request(first_request)

            other_mode = "deep" if initial_mode == "fast" else "fast"
            for index, mode in enumerate((other_mode, initial_mode), start=1):
                await session.stop_listening()
                assert episode.input_scope_closed is True
                assert await session.set_reasoning_mode(mode) is True
                assert session.snapshot().payload["reasoning_mode"] == mode
                assert controller.episode is episode
                assert episode.episode_id == identity
                if index == 1 and request_state != "completed":
                    assert episode.active_request is first_request
                    assert not first_request.done()
                await session.start_listening()
                assert episode.input_scope_closed is False
                if index == 1 and request_state != "completed":
                    assert episode.active_request is first_request
                    assert not first_request.done()
                    release.set()
                    assert await first_request == "completed"
                    await episode._finish_active_request(first_request)

                turn_id = f"turn-{index}"
                session.conversation.begin_turn(turn_id)
                episode.last_turn_id = turn_id
                episode.inputs[turn_id] = {"text": "继续回答", "final": True}
                session.history.commit_input(turn_id, "继续回答")
                assert await episode._start_dispatch({"USER_INPUT_FINALIZED"}) is True
                request = episode.active_request
                assert await request == "completed"
                await episode._finish_active_request(request)
                assert_request_mode(requests[-1], mode)
                assert_request_mode(requests[0], initial_mode)
                assert episode.ledger is session.history
                assert episode.inputs["turn-a"]["text"] == "你好"
                assert [(entry.kind, entry.content) for entry in session.history.entries] == [
                    ("input", "你好"), *[("input", "继续回答")] * index,
                ]
            assert len(requests) == 3
        finally:
            release.set()
            if first_request is not None:
                await asyncio.gather(first_request, return_exceptions=True)

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
