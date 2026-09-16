from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from pathlib import Path

import pytest

from types import SimpleNamespace
from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.agent.shadow_registry import VoiceShadowRegistry
from voice_terminal.config import AppConfig, load_config
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.shadow_agent import (
    ShadowContextProjector, ShadowEvent, ShadowRuntime, ShadowToolExecutor, parse_shadow_wake,
    request_shadow_action_definition,
)
from voice_terminal.shadow_agent.events import ShadowEventBuffer
from voice_terminal.shadow_agent.component import ShadowAgentComponent, _EventRelay
from voice_terminal.shadow_agent.contracts import shadow_control_tool_definitions
from voice_terminal.shadow_agent.provider import ShadowProviderResponse
from voice_terminal.shadow_agent.prompts import STATUS_OBSERVER_PROMPT
from voice_terminal.server.product import ShadowProductProjection
from voice_terminal.server.websocket import SessionRuntime


def message(text: str) -> dict:
    return {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": text}]}


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "function_call", "status": "completed", "call_id": call_id,
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }


class QueueProvider:
    def __init__(self, outputs):
        self.outputs = deque(outputs)
        self.requests = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        value = self.outputs.popleft()
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = await value(kwargs)
        return ShadowProviderResponse(tuple(value), {"input_tokens": 100, "cached_tokens": 75})


async def wait_idle(runtime: ShadowRuntime) -> None:
    for _ in range(100):
        if not runtime.active:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("runtime did not become idle")


def make_runtime(provider, ledger, tmp_path: Path, events, **options):
    return ShadowRuntime(
        provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
        instructions_loader=lambda: "fixed instructions", on_event=events.append,
        observer_enabled=False, **options,
    )


def test_request_shadow_contract_is_content_free_and_strict():
    definition = request_shadow_action_definition()
    assert definition["name"] == "request_shadow"
    assert definition["parameters"]["properties"] == {"request": {"type": "boolean", "const": True}}
    assert "不表示当前必须向用户说话" in definition["description"]
    assert parse_shadow_wake({"request": True}) is True
    for invalid in ({}, {"request": False}, {"request": True, "text": "x"}, True):
        with pytest.raises(ValueError):
            parse_shadow_wake(invalid)
    assert VoiceShadowRegistry(available=False).action_definitions() == []
    assert VoiceShadowRegistry(available=True).action_definitions() == [definition]


def test_shadow_event_prompts_report_facts_without_directing_main_speech():
    publish = next(
        item for item in shadow_control_tool_definitions()
        if item["name"] == "publish_main_event"
    )
    assert "只陈述事实及不确定性" in publish["description"]
    assert "不得指示主助手" in publish["description"]
    assert "不得建议或指示主助手说什么" in STATUS_OBSERVER_PROMPT
    assert "下一步向用户" in STATUS_OBSERVER_PROMPT
    assert "准确区分已完成、正在进行、下一步和阻塞" not in STATUS_OBSERVER_PROMPT


def test_configuration_exposes_unlimited_shadow_execution_defaults():
    cfg = AppConfig().shadow_agent
    assert cfg.max_context_tokens == 524288
    assert cfg.compaction_threshold_ratio == 0.80
    assert cfg.provider.timeout_ms is None
    assert cfg.provider.transport_retry_limit is None
    assert cfg.tools.timeout_ms == 15000
    assert cfg.tools.web_search.timeout_ms == 15000
    assert cfg.status_observer.timeout_ms is None
    assert cfg.status_observer.every_committed_steps == 5
    assert cfg.status_observer.reasoning_effort == "none"

    loaded = load_config(Path(__file__).parents[1] / "config" / "default.yaml").shadow_agent
    assert loaded.provider.timeout_ms is None
    assert loaded.provider.transport_retry_limit is None
    assert loaded.tools.timeout_ms == 15000
    assert loaded.tools.web_search.timeout_ms == 15000
    assert loaded.status_observer.timeout_ms is None


def test_shadow_runtime_has_no_provider_deadline_or_retry_count_ceiling(monkeypatch, tmp_path: Path):
    async def scenario():
        ledger, events = SessionLedger(), []
        ledger.commit_input("u1", "持续核对")
        provider = QueueProvider([
            RuntimeError("stream interrupted once"),
            RuntimeError("stream interrupted twice"),
            [message("无限重试后完成")],
        ])
        runtime = make_runtime(
            provider, ledger, tmp_path, events,
            provider_timeout_ms=None, transport_retry_limit=None,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert len(provider.requests) == 3
        assert events[-1].kind == "result"
        assert events[-1].text == "无限重试后完成"
        await runtime.close()

    async def forbidden_wait_for(*_args, **_kwargs):
        raise AssertionError("unlimited Shadow requests must not use asyncio.wait_for")

    monkeypatch.setattr(asyncio, "wait_for", forbidden_wait_for)
    asyncio.run(scenario())


def test_shadow_tool_calls_are_unbounded_by_default():
    class ConcurrentBackend:
        available = True

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.entered = 0
            self.max_seen = 0
            self.all_entered = threading.Event()

        def action_definitions(self):
            return [{
                "name": "files_search", "description": "test", "parameters": {
                    "type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False,
                },
            }]

        def normalize(self, name, arguments):
            return name, arguments

        def execute(self, _name, _arguments):
            with self.lock:
                self.entered += 1
                self.max_seen = max(self.max_seen, self.entered)
                if self.entered >= 3:
                    self.all_entered.set()
            if not self.all_entered.wait(timeout=1):
                raise RuntimeError("tool calls were serialized by a local concurrency cap")
            return {"ok": True}

    async def scenario():
        backend = ConcurrentBackend()
        executor = ShadowToolExecutor(backend)
        calls = [
            {"call_id": f"c{index}", "name": "files_search", "arguments": json.dumps({"query": str(index)})}
            for index in range(3)
        ]
        outputs = await asyncio.wait_for(executor.execute_calls(calls), timeout=2)
        assert backend.max_seen >= 3
        assert all(json.loads(item["output"]) == {"ok": True} for item in outputs)

    asyncio.run(scenario())


def test_shadow_tool_call_uses_configured_fifteen_second_deadline():
    class SlowBackend:
        available = True

        def action_definitions(self):
            return []

        def normalize(self, name, arguments):
            return name, arguments

        def execute(self, _name, _arguments):
            threading.Event().wait(0.05)
            return {"ok": True}

    async def scenario():
        executor = ShadowToolExecutor(SlowBackend(), timeout_ms=10)
        result = await executor.execute("files_search", {"query": "x"})
        assert result["error"] == "工具 files_search 执行超过 10ms"
        await asyncio.sleep(0.08)
        assert not executor._detached_work

    asyncio.run(scenario())


def test_shadow_projection_tracks_authoritative_facts_but_not_shadow_output():
    ledger = SessionLedger()
    ledger.commit_input("u1", "查一下说明书")
    ledger.update_input_refinement("u1", "查一下安装说明书")
    ledger.commit_delivery("s1", "我来查。", DeliveryReport(
        status="full", heard_text="我来查。", uncertain_text="", unheard_text="",
        precision="complete", termination="completed",
    ))
    before = ledger.shadow_input_revision
    ledger.commit_shadow_event(ShadowEvent("r1", "result", "找到文档", before, "priority"))
    ledger.commit_shadow_event(ShadowEvent("s1", "status", "正在读取", before, "normal"))
    assert before == 3
    assert ledger.shadow_input_revision == before
    snapshot = ledger.shadow_context_snapshot()
    assert [frame.kind for frame in snapshot.frames] == [
        "user_input", "asr_refinement", "delivered_speech",
    ]
    assert all("找到文档" not in json.dumps(frame.content, ensure_ascii=False) for frame in snapshot.frames)
    assert "影子 Agent 事件" in str(ledger.context_messages(""))


def test_projector_merges_unsettled_current_turn_and_freezes_environment():
    ledger = SessionLedger()
    assert ledger.project_shadow_current_turn("u1", "先查北京")
    assert not ledger.project_shadow_current_turn("u1", "先查北京")
    assert ledger.project_shadow_current_turn("u1", "改查上海")
    projector = ShadowContextProjector(ledger, user_profile="称呼用户为老大")
    first = projector.shadow_context_snapshot()
    second = projector.shadow_context_snapshot()
    assert first.frames[0] == second.frames[0]
    assert first.frames[0].kind == "session_environment"
    assert first.frames[0].content["user_profile"] == "称呼用户为老大"
    assert [frame.content["transcript"] for frame in first.frames[1:]] == ["先查北京", "改查上海"]


def test_main_tail_exposes_only_bounded_shadow_state():
    ledger = SessionLedger()
    ledger.set_shadow_runtime_state(available=True, active=True)
    ledger.commit_shadow_event(ShadowEvent("p", "progress", "正在核对第二个来源。", 0, "passive"))
    tail = LLMContextProjector.runtime_tail(
        current=CurrentInput(), speak_state={}, shadow=ledger.shadow_runtime_tail(),
    )
    assert "正在核对第二个来源" in tail
    assert "task" not in tail.lower()
    assert "revision" not in tail.lower()
    ledger.commit_shadow_event(ShadowEvent("r", "result", "完成", 0, "priority"))
    assert ledger.shadow_runtime_tail()["active"] is False


def test_main_action_passes_only_wake_and_projects_current_dispatch_input():
    async def scenario():
        class Registry:
            def action_definitions(self): return []
            def begin_turn(self, _turn): return None

        async def emit(*_args): return None

        ledger, revisions = SessionLedger(), []

        def wake():
            revisions.append(ledger.shadow_input_revision)
            return "started"

        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(status="IDLE"), emit,
            conversation=ConversationRuntime(), ledger=ledger, shadow_waker=wake,
        )
        runtime.inputs["u1"] = {"text": "当前仍在说的查询原文", "final": False}
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "我来处理。"),
            (Action("request_shadow", {"request": True}),),
        )
        await runtime._dispatch_actions_v06(("c", "r"), "u1", envelope)
        await asyncio.gather(*tuple(runtime._background_tools))
        assert revisions == [1]
        frame = ledger.shadow_context_snapshot().frames[-1]
        assert frame.kind == "current_dispatch_input"
        assert frame.content["transcript"] == "当前仍在说的查询原文"

    asyncio.run(scenario())


def test_event_buffer_overwrites_status_and_preserves_semantic_events():
    buffer = ShadowEventBuffer()
    buffer.publish(ShadowEvent("p1", "progress", "旧", 1, "passive"))
    buffer.publish(ShadowEvent("p2", "progress", "新", 1, "passive"))
    buffer.publish(ShadowEvent("o1", "observation", "发现一", 1, "normal"))
    buffer.publish(ShadowEvent("o2", "observation", "发现二", 1, "normal"))
    assert [(item.kind, item.text) for item in buffer.drain()] == [
        ("progress", "新"), ("observation", "发现一"), ("observation", "发现二"),
    ]


def test_event_buffer_restore_keeps_newer_visible_state_and_semantic_order():
    buffer = ShadowEventBuffer()
    old_status = ShadowEvent("old-status", "status", "旧状态", 1, "normal")
    old_result = ShadowEvent("old-result", "result", "旧结果", 1, "priority")
    buffer.publish(old_status)
    buffer.publish(old_result)
    in_flight = buffer.drain()
    buffer.publish(ShadowEvent("new-status", "status", "新状态", 2, "normal"))
    buffer.publish(ShadowEvent("new-observation", "observation", "新发现", 2, "normal"))
    buffer.restore(in_flight)
    assert [(item.event_id, item.text) for item in buffer.drain()] == [
        ("new-status", "新状态"),
        ("old-result", "旧结果"),
        ("new-observation", "新发现"),
    ]


def test_event_relay_retries_without_losing_terminal_event_and_ledger_is_idempotent():
    async def scenario():
        attempts: list[list[str]] = []
        metrics: list[tuple[str, dict]] = []
        ledger = SessionLedger()
        accepted = asyncio.Event()

        async def sink(events):
            attempts.append([item.event_id for item in events])
            for item in events:
                ledger.commit_shadow_event(item)
            if len(attempts) == 1:
                # Model a failure after the sink accepted the item but before
                # it could acknowledge the relay's complete batch.
                raise RuntimeError("temporary voice-side failure")
            accepted.set()

        relay = _EventRelay(
            sink, on_metric=lambda name, payload: metrics.append((name, payload)),
            retry_initial_delay_seconds=0.001, retry_max_delay_seconds=0.002,
        )
        terminal = ShadowEvent("terminal", "result", "完成", 1, "priority")
        relay.publish(terminal)
        await asyncio.wait_for(accepted.wait(), timeout=1.0)
        await asyncio.gather(relay.task, return_exceptions=False)
        assert attempts == [["terminal"], ["terminal"]]
        assert [entry for entry in ledger.entries if entry.kind == "shadow_event"]
        assert len([entry for entry in ledger.entries if entry.kind == "shadow_event"]) == 1
        assert any(name == "event.delivery_retry" for name, _ in metrics)
        assert any(name == "event.delivered" for name, _ in metrics)
        await relay.close()

    asyncio.run(scenario())


def test_experimental_gate_defers_once_then_rechecks_full_unreleased_prefix(tmp_path: Path):
    class BlockingProvider:
        def __init__(self):
            self.started = asyncio.Event()

        async def complete(self, **kwargs):
            stream = kwargs.get("on_stream")
            if stream is not None:
                await stream({"type": "response.reasoning_text.delta", "delta": "正在核对原始任务"})
            self.started.set()
            await asyncio.Event().wait()

    async def scenario():
        ledger = SessionLedger()
        ledger.commit_input("u1", "初始任务")
        projector = ShadowContextProjector(ledger)
        main_provider = BlockingProvider()
        status_provider = QueueProvider([
            [message("已确认进展：初始任务已开始；当前关注：核对原始任务。")],
            [message(json.dumps({
                "timing": "defer_once", "relation": "irrelevant", "reason": "与当前工作无关",
            }, ensure_ascii=False))],
            [message(json.dumps({
                "timing": "inject_now", "relation": "guidance", "reason": "补充了明确引导",
            }, ensure_ascii=False))],
            [message("not-json")],
        ])
        metrics = []
        component = ShadowAgentComponent(
            main_provider, ShadowToolExecutor(FileTools((tmp_path,))),
            status_provider=status_provider,
            instructions_loader=lambda: "fixed",
            runtime_options={
                "observer_enabled": True, "observer_every_steps": 99,
                "on_metric": lambda name, payload: metrics.append((name, payload)),
            },
        )
        session = component.new_session(projector)
        session.set_context_gate_enabled(True)
        assert await session.request_wake() == "started"
        await asyncio.wait_for(main_provider.started.wait(), timeout=1)

        ledger.commit_input("u2", "第一条运行中正文")
        assert await session.request_wake() == "coalesced"
        assert projector.visible_revision == 2
        for _ in range(100):
            if session.runtime.has_current_summary:
                break
            await asyncio.sleep(0.01)
        assert session.runtime.has_current_summary
        forced_request = status_provider.requests[0]
        assert "正在核对原始任务" in json.dumps(forced_request["input_items"], ensure_ascii=False)
        assert "正在核对原始任务" not in repr(session.runtime.working_trace)

        ledger.commit_input("u3", "本次可以暂缓的闲话")
        assert await session.request_wake() == "coalesced"
        assert projector.visible_revision == 2

        ledger.commit_input("u4", "新的明确引导")
        assert await session.request_wake() == "coalesced"
        assert projector.visible_revision == 4
        gate_payload = json.loads(status_provider.requests[-1]["input_items"][0]["content"])
        assert gate_payload["candidate"]["from_revision"] == 2
        assert gate_payload["candidate"]["to_revision"] == 4
        assert [frame["content"]["transcript"] for frame in gate_payload["candidate"]["frames"]] == [
            "本次可以暂缓的闲话", "新的明确引导",
        ]
        assert all("shadow_summary" not in repr(frame)
                   for frame in projector.shadow_context_snapshot().frames)

        ledger.commit_input("u5", "非法门禁输出也不能丢失")
        assert await session.request_wake() == "coalesced"
        assert projector.visible_revision == 5
        assert any(name == "context_gate.fail_open" for name, _ in metrics)
        await component.close()
        assert session.runtime._reasoning_stream_tail == ""

    asyncio.run(scenario())


def test_disabling_context_gate_releases_deferred_authority_prefix():
    ledger = SessionLedger()
    ledger.commit_input("u1", "一")
    projector = ShadowContextProjector(ledger)
    projector.set_gate_enabled(True)
    ledger.commit_input("u2", "二")
    assert projector.visible_revision == 1
    projector.set_gate_enabled(False)
    assert projector.visible_revision == 2
    assert [frame.content["transcript"] for frame in projector.shadow_context_snapshot().frames[1:]] == ["一", "二"]


def test_status_and_progress_are_product_only_while_semantic_events_use_mailbox():
    async def scenario():
        runtime = SessionRuntime(AppConfig())
        status = ShadowEvent("s", "status", "正在整理", 1, "normal")
        progress = ShadowEvent("p", "progress", "已读取文件", 1, "normal")
        await runtime._on_shadow_events([status, progress])
        assert runtime.shadow_product.summary == "已读取文件"
        assert runtime._shadow_pending.drain() == []

        result = ShadowEvent("r", "result", "整理完成", 1, "priority")
        await runtime._on_shadow_events([result])
        assert [item.event_id for item in runtime._shadow_pending.drain()] == ["r"]

    asyncio.run(scenario())


def test_shadow_activity_stream_merges_items_and_discards_stale_runs():
    projection = ShadowProductProjection()
    projection.update_activities([
        {"run_seq": 4, "item_id": "thinking-4", "stream": "thinking", "phase": "started", "text": "正在", "timestamp": "2026-09-07T10:00:00Z"},
        {"run_seq": 4, "item_id": "thinking-4", "stream": "thinking", "phase": "delta", "delta": "分析", "timestamp": "2026-09-07T10:00:01Z"},
        {"run_seq": 4, "item_id": "thinking-4", "stream": "thinking", "phase": "completed", "timestamp": "2026-09-07T10:00:01Z"},
        {"run_seq": 4, "item_id": "tool-4", "stream": "tool", "phase": "started", "tool_name": "files_search", "tool_status": "running", "timestamp": "2026-09-07T10:00:02Z"},
        {"run_seq": 4, "item_id": "tool-4", "stream": "tool", "phase": "completed", "tool_name": "files_search", "tool_status": "completed", "text": "工具执行完成", "timestamp": "2026-09-07T10:00:03Z"},
    ])
    projection.update_activities([
        {"run_seq": 3, "item_id": "late", "stream": "event", "phase": "completed", "event_kind": "result", "text": "旧运行", "timestamp": "2026-09-07T09:59:00Z"},
    ])
    snapshot = projection.snapshot()
    assert [item["item_id"] for item in snapshot["activities"]] == ["thinking-4", "tool-4"]
    assert snapshot["activities"][0]["text"] == "正在分析"
    assert snapshot["activities"][1]["tool_status"] == "completed"
    projection.update_activities([
        {"run_seq": 4, "item_id": "tool-4", "stream": "tool", "phase": "delta", "tool_status": "running", "timestamp": "2026-09-07T10:00:04Z"},
    ])
    assert projection.snapshot()["activities"][1]["tool_status"] == "completed"

    projection.update_activities([
        {"run_seq": 5, "item_id": "run-5", "stream": "lifecycle", "phase": "started", "text": "新运行", "timestamp": "2026-09-07T10:01:00Z"},
    ])
    activity_ids = [item["item_id"] for item in projection.snapshot()["activities"]]
    assert activity_ids[-1] == "run-5"
    assert activity_ids[:2] == ["thinking-4", "tool-4"]


def test_shadow_runtime_emits_separate_activity_stream_without_trace_deltas(tmp_path: Path):
    async def scenario():
        ledger, events, activities = SessionLedger(), [], []
        ledger.commit_input("u1", "查找说明")
        provider = QueueProvider([
            [call("event", "publish_main_event", {"kind": "progress", "text": "开始查找"})],
            [message("最终完成")],
        ])
        runtime = make_runtime(
            provider, ledger, tmp_path, events, on_activity=activities.append,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert events[-1].kind == "result"
        assert any(item["stream"] == "thinking" for item in activities)
        assert any(item["stream"] == "tool" and item.get("tool_name") == "publish_main_event" for item in activities)
        assert any(item["stream"] == "event" and item.get("event_kind") == "progress" for item in activities)
        assert any(item["stream"] == "lifecycle" and item["phase"] == "completed" for item in activities)
        assert all("private" not in json.dumps(item, ensure_ascii=False) for item in activities)
        assert all("run_seq" not in item and "stream" not in item for item in runtime.working_trace)
        await runtime.close()

    asyncio.run(scenario())


def test_shadow_provider_retry_closes_unfinished_activity_items(tmp_path: Path):
    class StreamingRetryProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                callback = kwargs.get("on_stream")
                assert callback is not None
                await callback({
                    "type": "response.output_item.added",
                    "item": {"id": "provider-call", "type": "function_call", "call_id": "provider-call", "name": "files_search"},
                })
                raise RuntimeError("stream disconnected")
            return ShadowProviderResponse((message("重试完成"),), {"input_tokens": 12})

    async def scenario():
        ledger, events, activities = SessionLedger(), [], []
        ledger.commit_input("u1", "查找说明")
        runtime = make_runtime(
            StreamingRetryProvider(), ledger, tmp_path, events,
            on_activity=activities.append, transport_retry_limit=1,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert events[-1].kind == "result"
        failed_tools = [
            item for item in activities
            if item["stream"] == "tool" and item["phase"] == "failed"
        ]
        assert failed_tools
        assert any(item["stream"] == "lifecycle" and item["phase"] == "failed" for item in activities)
        assert any(item["stream"] == "lifecycle" and item["phase"] == "completed" for item in activities)
        await runtime.close()

    asyncio.run(scenario())


def test_wake_is_single_runner_coalesced_and_same_revision_idempotent(tmp_path: Path):
    async def scenario():
        gate = asyncio.Event()

        async def blocked(_kwargs):
            await gate.wait()
            return [message('{"state":"no_action","text":""}')]

        ledger = SessionLedger()
        ledger.commit_input("u1", "检查")
        runtime = make_runtime(QueueProvider([blocked]), ledger, tmp_path, [])
        assert runtime.wake() == "started"
        assert runtime.wake() == "coalesced"
        runner = runtime.runner
        gate.set()
        await wait_idle(runtime)
        assert runtime.runner is runner
        assert runtime.wake() == "already_current"
        await runtime.close()

    asyncio.run(scenario())


def test_native_tool_roundtrip_keeps_exact_call_output_order(tmp_path: Path):
    async def scenario():
        (tmp_path / "note.md").write_text("shadow fact", encoding="utf-8")
        provider = QueueProvider([
            [call("a", "files_search", {"query": "shadow fact"}), call("b", "files_read_text", {"path": "note.md"})],
            [message("找到了 note.md")],
        ])
        ledger, events = SessionLedger(), []
        ledger.commit_input("u1", "查找 shadow fact")
        runtime = make_runtime(provider, ledger, tmp_path, events)
        runtime.wake()
        await wait_idle(runtime)
        second = provider.requests[1]["input_items"]
        outputs = [item for item in second if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["a", "b"]
        assert events[-1].kind == "result" and events[-1].text == "找到了 note.md"
        await runtime.close()

    asyncio.run(scenario())


def test_context_update_prevents_unstarted_old_tool_and_reconciles(tmp_path: Path):
    async def scenario():
        ledger, events = SessionLedger(), []

        async def stale(_kwargs):
            ledger.commit_input("u2", "改成查上海")
            runtime.wake()
            return [call("old", "files_search", {"query": "北京"})]

        provider = QueueProvider([stale, [message("上海结果")]])
        runtime = make_runtime(provider, ledger, tmp_path, events)
        ledger.commit_input("u1", "查北京")
        runtime.wake()
        await wait_idle(runtime)
        second = provider.requests[1]["input_items"]
        denied = [item for item in second if item.get("type") == "function_call_output"]
        assert json.loads(denied[-1]["output"]) == {
            "status": "not_executed", "reason": "conversation_context_updated",
        }
        assert "上海" in json.dumps(second, ensure_ascii=False)
        assert [event.text for event in events if event.kind == "result"] == ["上海结果"]
        await runtime.close()

    asyncio.run(scenario())


def test_clarification_ends_activation_with_context_only_correlation(tmp_path: Path):
    async def scenario():
        ledger, events = SessionLedger(), []
        ledger.commit_input("u1", "查文档")
        provider = QueueProvider([[
            call("q", "request_user_input", {
                "kind": "clarification", "question": "查哪个目录？",
                "options": ["docs", "src"], "operation_summary": "",
            })
        ]])
        runtime = make_runtime(provider, ledger, tmp_path, events)
        runtime.wake()
        await wait_idle(runtime)
        event = events[-1]
        assert event.kind == "user_input_required"
        assert event.payload == {"kind": "clarification", "options": ["docs", "src"], "operation_summary": ""}
        assert set(event.payload) == {"kind", "options", "operation_summary"}
        await runtime.close()

    asyncio.run(scenario())


def test_transport_retry_exhaustion_fails_only_activation(tmp_path: Path):
    async def scenario():
        ledger, events = SessionLedger(), []
        ledger.commit_input("u1", "查询")
        provider = QueueProvider([RuntimeError("one"), RuntimeError("two")])
        runtime = make_runtime(provider, ledger, tmp_path, events, transport_retry_limit=1)
        runtime.wake()
        await wait_idle(runtime)
        assert runtime.available and events[-1].kind == "failed"
        assert len(provider.requests) == 2
        await runtime.close()

    asyncio.run(scenario())
