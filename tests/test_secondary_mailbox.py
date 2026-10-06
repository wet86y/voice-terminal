"""二等信箱恢复：验证当前桥接协议下的来源、激活、请求冻结和重试边界。"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_scheduler_staircase import (
    _BlockingProvider, _finish_blocking_request, _shadow_progress, _speaking_runtime,
)
from voice_terminal.agent.runtime_events import HeartbeatPulse
from voice_terminal.config import AppConfig
from voice_terminal.dsh.bridge import DshMappingStore, DshShadowAgentSession
from voice_terminal.providers.deepseek import DeepSeekTransportError
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.context_frames import ContextLane
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.shadow_agent.context import ShadowContextProjector
from voice_terminal.shadow_agent.contracts import ShadowEvent
from voice_terminal.shadow_agent.events import ShadowEventBuffer


class CapturingProvider(_BlockingProvider):
    def __init__(self):
        super().__init__()
        self.messages = []

    def stream_events(self, messages, *, json_mode=False):
        self.messages.append(messages)
        return super().stream_events(messages, json_mode=json_mode)


def tail_entries(messages):
    packet = json.loads(messages[-2]["content"])
    return {entry["kind"]: entry["content"] for entry in packet["entries"]}


def shadow_state(messages):
    content = tail_entries(messages)["runtime_state"].split("\n工具可用性：")[0]
    return json.loads(content.removeprefix("当前轮运行状态："))["shadow"]


async def wait_for_calls(provider, count):
    for _ in range(20):
        if provider.calls >= count:
            return
        await asyncio.sleep(0)
    assert provider.calls == count


def test_empty_mailbox_never_invokes_at_secondary_checkpoints():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        for checkpoint in range(4, 29, 4):
            await runtime._on_heartbeat(float(checkpoint))
            await asyncio.sleep(0)
        assert provider.calls == 0
        assert runtime.active_request is None
        assert runtime.scheduler.anchor_monotonic == 0.0

    asyncio.run(scenario())


def test_latest_progress_from_each_source_is_frozen_once_and_does_not_change_history():
    async def scenario():
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        runtime.ledger.commit_input("turn-a", "继续核对")
        before = runtime.messages({"USER_INPUT_FINALIZED"})
        await _shadow_progress(runtime, "代理旧状态", "p1")
        await _shadow_progress(runtime, "观察器最新状态", "p2", source="observer")
        await _shadow_progress(runtime, "代理最新状态", "p3")
        await runtime._on_heartbeat(4.0)
        await wait_for_calls(provider, 1)
        messages = provider.messages[-1]
        assert shadow_state(messages)["progress"] == [
            {"text": "代理最新状态", "source": "agent"},
            {"text": "观察器最新状态", "source": "observer"},
        ]
        for lane in (ContextLane.STATIC_INSTRUCTIONS, ContextLane.HISTORY):
            assert [item for item in messages if item["context_lane"] == lane] == [
                item for item in before if item["context_lane"] == lane
            ]
        assert "代理旧状态" not in json.dumps(messages, ensure_ascii=False)
        assert runtime._secondary_pending == {}
        assert runtime.snapshot()["secondary_mailbox"]["consumed_check"] == 2
        await _finish_blocking_request(runtime, provider)
        await _shadow_progress(runtime, "  代理最新状态  ", "p4")
        await _shadow_progress(runtime, "观察器最新状态", "p5", source="observer")
        await runtime._on_heartbeat(8.0)
        assert provider.calls == 1
        assert runtime.max_request_concurrency == 1
        assert runtime.scheduler.anchor_monotonic == 0.0
        assert runtime.scheduler.generation == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("progress_first", [True, False])
def test_same_information_and_progress_use_only_the_ordinary_entry(progress_first):
    async def scenario():
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime._ensure_runner = lambda: None
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        info = ShadowEvent("i1", "information", "已完成核对", 1, "activation-a", source="observer")
        if progress_first:
            await _shadow_progress(runtime, "已完成核对", "p1")
        await runtime.accept_shadow_event(info)
        if not progress_first:
            await _shadow_progress(runtime, "已完成核对", "p1")
        assert runtime.pending == {"SHADOW_INFORMATION"}
        assert runtime._secondary_pending == {}
        await runtime._on_heartbeat(4.0)
        await wait_for_calls(provider, 1)
        assert runtime.active_dispatch_context is None
        assert shadow_state(provider.messages[-1])["information"]["text"] == "已完成核对"
        assert "progress" not in shadow_state(provider.messages[-1])
        await _finish_blocking_request(runtime, provider)
        await _shadow_progress(runtime, "已完成核对", "p2")
        await runtime._on_heartbeat(8.0)
        assert provider.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["messages", "fit", "audit"])
def test_failed_context_freeze_keeps_the_pending_progress(failure_stage):
    async def scenario():
        runtime = _speaking_runtime(_BlockingProvider())
        await _shadow_progress(runtime, "等待评估", "p1")

        def fail(*_args, **_kwargs):
            raise RuntimeError("freeze failed")

        if failure_stage == "messages":
            runtime.messages = fail
        elif failure_stage == "fit":
            runtime._fit_context = fail
        audit_patch = patch("voice_terminal.agent.episode.capture_context_snapshot", fail) if failure_stage == "audit" else nullcontext()
        with audit_patch:
            with pytest.raises(RuntimeError, match="freeze failed"):
                await runtime._step((runtime.episode_id, "request-1"), {"USER_INPUT_FINALIZED"})
        assert runtime._secondary_pending == {"agent": 1}
        assert runtime._retry_secondary_by_turn == {}

    asyncio.run(scenario())


def test_information_repeating_already_frozen_progress_does_not_invoke_again():
    async def scenario():
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime._ensure_runner = lambda: None
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "已核对完毕", "p1")
        await runtime._on_heartbeat(4.0)
        await wait_for_calls(provider, 1)
        await _finish_blocking_request(runtime, provider)
        await runtime.accept_shadow_event(ShadowEvent("i1", "information", "已核对完毕", 1, "activation-a"))
        assert runtime.pending == set()
        assert runtime.ledger.shadow_information_snapshot() is None
        await runtime._on_heartbeat(8.0)
        assert provider.calls == 1

    asyncio.run(scenario())


def test_activation_change_rejects_old_sources_and_old_version_confirmation():
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True, activation_id="a")
    ledger.commit_shadow_event(ShadowEvent("p1", "progress", "第一激活", 1, "a"))
    old = ledger.shadow_progress_snapshot()
    ledger.set_shadow_runtime_state(active=True, activation_id="b")
    ledger.commit_shadow_event(ShadowEvent("late-a", "progress", "旧激活迟到", 1, "a"))
    ledger.commit_shadow_event(ShadowEvent("bad-source", "progress", "错误来源", 1, "b", source="invalid"))
    ledger.commit_shadow_event(ShadowEvent("missing-scope", "progress", "缺少激活", 1))
    assert ledger.shadow_progress_snapshot() == {}
    ledger.commit_shadow_event(ShadowEvent("p2", "progress", "第二激活", 1, "b"))
    assert ledger.consume_shadow_progress(old) == []
    assert ledger.valid_shadow_progress(old) == {}
    assert ledger.shadow_progress_snapshot()["agent"]["text"] == "第二激活"
    # 旧终态的语义可保留，但不能关闭新激活的进度作用域。
    ledger.commit_shadow_event(ShadowEvent("old-result", "result", "旧结果", 1, "a"))
    assert ledger.shadow_progress_snapshot()["agent"]["text"] == "第二激活"


@pytest.mark.parametrize("terminal_kind", ["result", "input_required", "no_action", "failed"])
def test_terminal_clears_progress_and_late_progress_cannot_reopen_it(terminal_kind):
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True, activation_id="a")
    ledger.commit_shadow_event(ShadowEvent("p1", "progress", "旧进度", 1, "a"))
    ledger.commit_shadow_event(ShadowEvent("t1", terminal_kind, "任务结束", 1, "a"))
    ledger.commit_shadow_event(ShadowEvent("p2", "progress", "迟到进度", 1, "a"))
    assert ledger.shadow_progress_snapshot() == {}
    assert ledger.shadow_active is False


@pytest.mark.parametrize("boundary", ["close_input_scope", "reset", "stop", "unavailable"])
def test_lifecycle_boundary_clears_progress_and_frozen_scope(boundary):
    async def scenario():
        runtime = _speaking_runtime(_BlockingProvider())
        await _shadow_progress(runtime, "待处理", "p1")
        captured = runtime._secondary_snapshot()
        if boundary == "close_input_scope":
            runtime.close_input_scope()
            await _shadow_progress(runtime, "停止后进度", "p2")
        elif boundary == "reset":
            runtime.ledger.reset()
        elif boundary == "stop":
            await runtime.stop()
            await _shadow_progress(runtime, "关闭后进度", "p2")
        else:
            runtime.ledger.set_shadow_runtime_state(available=False)
        assert runtime._secondary_pending == {}
        assert runtime.ledger.valid_shadow_progress(captured) == {}

    asyncio.run(scenario())


def test_progress_and_fingerprints_are_not_restored_from_storage(tmp_path):
    directory = tmp_path / "ledger"
    ledger = SessionLedger(storage_dir=directory)
    ledger.set_shadow_runtime_state(available=True, active=True, activation_id="a")
    ledger.commit_shadow_event(ShadowEvent("p1", "progress", "临时进度", 1, "a"))
    assert ledger.shadow_progress_snapshot()
    assert ledger.context_messages("") == []
    assert ledger.shadow_context_snapshot().frames == ()
    ledger.close_storage()
    restored = SessionLedger(storage_dir=directory)
    assert restored.shadow_progress_snapshot() == {}
    assert restored._shadow_progress_seen == {}
    assert restored.context_messages("") == []
    restored.close_storage()


def test_progress_buffer_coalesces_per_activation_and_source():
    buffer = ShadowEventBuffer()
    for event in (
        ShadowEvent("p1", "progress", "代理旧", 1, "a"),
        ShadowEvent("p2", "progress", "观察器新", 1, "a", source="observer"),
        ShadowEvent("p3", "progress", "代理新", 1, "a"),
    ):
        buffer.publish(event)
    assert [(event.source, event.text) for event in buffer.drain()] == [
        ("agent", "代理新"), ("observer", "观察器新"),
    ]


class RetryProvider(CapturingProvider):
    def stream_events(self, messages, *, json_mode=False):
        self.messages.append(messages)
        self.calls += 1
        attempt = self.calls

        async def stream():
            if attempt == 1:
                raise DeepSeekTransportError("timeout", received_content=False)
            await self.release.wait()
            yield {"type": "content", "text": '{"speak":{"state":"wait"},"actions":[]}'}
            yield {"type": "finish", "reason": "stop"}

        return stream()


async def fail_secondary_request(runtime, provider):
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await _shadow_progress(runtime, "原请求进度", "p1")
    await runtime._on_heartbeat(4.0)
    request = runtime.active_request
    assert request is not None
    with pytest.raises(DeepSeekTransportError):
        await request
    context = dict(runtime.active_dispatch_context)
    await runtime._finish_active_request(request)
    assert runtime.pending == {"LLM_RETRY"}
    assert runtime._retry_system_check is not None
    assert provider.calls == 1
    return context


def test_transport_retry_keeps_secondary_scope_and_does_not_consume_new_progress():
    async def scenario():
        provider = RetryProvider()
        runtime = _speaking_runtime(provider)
        original = await fail_secondary_request(runtime, provider)
        await _shadow_progress(runtime, "后来新进度", "p2")
        runtime.backoff_until = 0
        await runtime._on_heartbeat(4.4)
        await wait_for_calls(provider, 2)
        assert runtime.active_dispatch_context["prompt_kind"] == "secondary_mailbox"
        assert runtime.active_dispatch_context["system_check_id"] == original["system_check_id"]
        assert shadow_state(provider.messages[-1])["progress"] == [{"text": "原请求进度", "source": "agent"}]
        assert runtime._secondary_snapshot()["agent"]["text"] == "后来新进度"
        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(8.0)
        await wait_for_calls(provider, 3)
        assert shadow_state(provider.messages[-1])["progress"] == [{"text": "后来新进度", "source": "agent"}]
        await _finish_blocking_request(runtime, provider)
        assert runtime.max_request_concurrency == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("next_event", ["USER_INPUT_FINALIZED", "final_deadline", "activation_expired"])
def test_secondary_retry_yields_to_new_input_final_deadline_or_expired_activation(next_event):
    async def scenario():
        provider = RetryProvider()
        runtime = _speaking_runtime(provider)
        await fail_secondary_request(runtime, provider)
        runtime.backoff_until = 0
        if next_event == "USER_INPUT_FINALIZED":
            runtime.pending.add(next_event)
            await runtime._on_heartbeat(4.4)
            await wait_for_calls(provider, 2)
            assert runtime.active_dispatch_context is None
            assert "secondary_mailbox" not in tail_entries(provider.messages[-1])
        elif next_event == "final_deadline":
            await runtime._on_heartbeat(30.0)
            await wait_for_calls(provider, 2)
            assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
        else:
            runtime.ledger.set_shadow_runtime_state(active=False)
            await runtime._on_heartbeat(4.4)
            assert runtime.active_request is None
            assert provider.calls == 1
        assert runtime._retry_system_check is None
        if runtime.active_request is not None:
            await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_long_pause_evaluates_latest_state_once_without_replaying_checkpoints():
    async def scenario():
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "最新状态", "p1")
        await runtime._on_heartbeat(20.1)
        await wait_for_calls(provider, 1)
        await _finish_blocking_request(runtime, provider)
        await _shadow_progress(runtime, "稍后状态", "p2")
        await runtime._on_heartbeat(20.5)
        assert provider.calls == 1
        assert runtime._secondary_pending
        await runtime._on_heartbeat(24.0)
        await wait_for_calls(provider, 2)
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_delayed_heartbeat_uses_arrival_time_and_prioritizes_final_silence():
    async def scenario():
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "暂停后的最新状态", "p1")
        pulse = HeartbeatPulse(episode_generation=1, sequence=10, scheduled_at=4.0)
        await runtime._on_heartbeat(4.0, pulse=pulse, delivered_at=32.0)
        await wait_for_calls(provider, 1)
        assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
        assert runtime.scheduler.final_dispatched
        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(32.4)
        assert provider.calls == 1

    asyncio.run(scenario())


class ProtocolRecoveryProvider(CapturingProvider):
    def __init__(self, *, transport_failure=False):
        super().__init__()
        self.transport_failure = transport_failure

    def stream_events(self, messages, *, json_mode=False):
        self.messages.append(messages)
        self.calls += 1
        attempt = self.calls

        async def stream():
            if self.transport_failure and attempt == 2:
                raise DeepSeekTransportError("recovery timeout", received_content=False)
            text = "这不是合法决策" if attempt == 1 else '{"speak":{"state":"wait"},"actions":[]}'
            yield {"type": "content", "text": text}
            yield {"type": "finish", "reason": "stop"}

        return stream()


async def fail_secondary_protocol_request(runtime, provider):
    runtime._ensure_runner = lambda: None
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await _shadow_progress(runtime, "原评估进度", "p1")
    await runtime._on_heartbeat(4.0)
    request = runtime.active_request
    assert request is not None
    original = dict(runtime.active_dispatch_context)
    await request
    await runtime._finish_active_request(request)
    assert len(runtime.recovery_queue) == 1
    assert provider.calls == 1
    return original


def test_protocol_recovery_reuses_the_frozen_secondary_snapshot():
    async def scenario():
        provider = ProtocolRecoveryProvider()
        runtime = _speaking_runtime(provider)
        await fail_secondary_protocol_request(runtime, provider)
        await _shadow_progress(runtime, "恢复期间新进度", "p2")
        assert await runtime._start_recovery()
        recovery = runtime.active_request
        assert await recovery == "completed"
        await runtime._finish_active_request(recovery)
        entries = tail_entries(provider.messages[-1])
        assert "secondary_mailbox" in entries
        assert "不要重新回答历史用户请求" in entries["immediate_recovery"]
        assert shadow_state(provider.messages[-1])["progress"] == [{"text": "原评估进度", "source": "agent"}]
        assert runtime._secondary_snapshot()["agent"]["text"] == "恢复期间新进度"
        assert runtime.max_request_concurrency == 1

    asyncio.run(scenario())


def test_transport_retry_after_protocol_recovery_keeps_secondary_scope():
    async def scenario():
        provider = ProtocolRecoveryProvider(transport_failure=True)
        runtime = _speaking_runtime(provider)
        original = await fail_secondary_protocol_request(runtime, provider)
        await _shadow_progress(runtime, "恢复期间新进度", "p2")
        assert await runtime._start_recovery()
        recovery = runtime.active_request
        with pytest.raises(DeepSeekTransportError):
            await recovery
        await runtime._finish_active_request(recovery)
        assert runtime.pending == {"LLM_RETRY"}
        assert runtime._retry_system_check["origin_prompt_kind"] == "secondary_mailbox"
        runtime.backoff_until = 0
        await runtime._on_heartbeat(4.4)
        retry = runtime.active_request
        assert retry is not None
        assert await retry == "completed"
        assert runtime.active_dispatch_context["system_check_id"] == original["system_check_id"]
        assert "secondary_mailbox" in tail_entries(provider.messages[-1])
        assert shadow_state(provider.messages[-1])["progress"] == [{"text": "原评估进度", "source": "agent"}]
        assert runtime._secondary_snapshot()["agent"]["text"] == "恢复期间新进度"
        await runtime._finish_active_request(retry)
        assert provider.calls == 3
        assert runtime.max_request_concurrency == 1
        assert runtime._retry_system_check is None

    asyncio.run(scenario())


def test_expired_activation_discards_secondary_protocol_recovery():
    async def scenario():
        provider = ProtocolRecoveryProvider()
        runtime = _speaking_runtime(provider)
        original = await fail_secondary_protocol_request(runtime, provider)
        runtime.ledger.set_shadow_runtime_state(active=False)
        assert await runtime._start_recovery() is False
        assert provider.calls == 1
        assert runtime.active_request is None
        assert not runtime.recovery_queue
        assert runtime._retry_secondary_by_turn == {}
        assert original["system_check_id"] not in runtime._system_check_turn_ids

    asyncio.run(scenario())


def test_live_server_bridge_delivers_progress_to_secondary_and_information_to_normal(tmp_path):
    async def scenario():
        session = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "runtime")
        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.ledger = session.history
        runtime._ensure_runner = lambda: None
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")

        async def accept(events):
            for event in events:
                await runtime.accept_shadow_event(event)

        class Client:
            async def call(self, _operation, _payload):
                return {"protocol_version": "2.0", "result": "accepted"}

        bridge = DshShadowAgentSession(
            client=Client(), context_source=ShadowContextProjector(session.history),
            mapping_store=DshMappingStore(tmp_path / "mapping.json"),
            voice_session_id=session.conversation.conversation_id,
            workspace=tmp_path,
            production_sink=session._on_shadow_events,
        )
        session.shadow_session = bridge
        session.controller = SimpleNamespace(episode=runtime, accept_shadow_events=accept)
        session.state.listening = True
        await bridge.request_wake()
        base = bridge._envelope(protocol_version="2.0", context_revision=bridge.last_applied_revision,
                                activation_id=bridge.activation_id)
        progress = {**base, "request_id": "request-p", "event_id": "p", "event": {"type": "progress", "text": "真实投递进度"}}
        assert await bridge.accept_event(progress) == "accepted"
        assert session.shadow_product.summary == "真实投递进度"
        assert runtime.pending == set()
        assert runtime._secondary_snapshot()["agent"]["text"] == "真实投递进度"
        await runtime._on_heartbeat(4.0)
        await wait_for_calls(provider, 1)
        assert "secondary_mailbox" in tail_entries(provider.messages[-1])
        await _finish_blocking_request(runtime, provider)
        info = {**base, "request_id": "request-i", "event_id": "i", "event": {"type": "information", "text": "立即普通信息"}}
        assert await bridge.accept_event(info) == "accepted"
        assert runtime.pending == {"SHADOW_INFORMATION"}
        await runtime._on_heartbeat(4.4)
        await wait_for_calls(provider, 2)
        assert runtime.active_dispatch_context is None
        assert shadow_state(provider.messages[-1])["information"]["text"] == "立即普通信息"
        await _finish_blocking_request(runtime, provider)
        assert runtime.max_request_concurrency == 1
        await bridge.cancel()
        assert session.history.shadow_progress_snapshot() == {}
        await bridge.close()

    asyncio.run(scenario())
