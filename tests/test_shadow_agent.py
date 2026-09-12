from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.config import AppConfig
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.shadow_agent import ShadowTaskCommand, ShadowTaskManager, ShadowToolExecutor, task_action_definition
from voice_terminal.shadow_agent.events import ShadowTaskEventBuffer
from voice_terminal.shadow_agent.contracts import ShadowTaskEvent
from voice_terminal.shadow_agent.provider import ShadowProviderResponse


class Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ScriptedProvider:
    def __init__(self, responses, *, clock: Clock | None = None, advance_on_call: float = 0.0):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock
        self.advance_on_call = advance_on_call

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.clock is not None:
            self.clock.advance(self.advance_on_call)
        next_value = self.responses.pop(0)
        if isinstance(next_value, BaseException):
            raise next_value
        if callable(next_value):
            return await next_value(kwargs)
        return next_value


def message(text: str) -> ShadowProviderResponse:
    return ShadowProviderResponse(({
        "type": "message", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    },), {})


def function_call(call_id="call_1", name="files_search", arguments='{"query":"note"}') -> ShadowProviderResponse:
    return ShadowProviderResponse(({
        "type": "function_call", "status": "completed", "call_id": call_id,
        "name": name, "arguments": arguments,
    },), {})


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


async def wait_terminal(manager, identifier: int):
    for _ in range(200):
        snapshot = manager.snapshot(identifier)
        if snapshot is not None and snapshot["status"] in {"completed", "cancelled", "timed_out", "failed"}:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("shadow task did not reach a terminal state")


def make_manager(tmp_path, provider, *, clock=None, **kwargs):
    tools = ShadowToolExecutor(FileTools((tmp_path,)))
    return ShadowTaskManager(
        provider, tools, clock=clock or Clock(), instructions_loader=lambda: "shadow rules", **kwargs,
    )


def test_task_contract_is_strict_and_provider_ready():
    definition = task_action_definition()
    assert definition["name"] == "task"
    assert definition["parameters"]["required"] == ["id", "state", "text"]
    assert ShadowTaskCommand.parse({"id": 1, "state": "cancel", "text": ""}).id == 1
    with pytest.raises(ValueError):
        ShadowTaskCommand.parse({"id": 1, "state": "request"})
    with pytest.raises(ValueError):
        ShadowTaskCommand.parse({"id": True, "state": "request", "text": "x"})


def test_shadow_configuration_defaults_and_legacy_tools_are_rejected():
    config = AppConfig.from_mapping({"llm": {"base_url": "https://example.invalid", "model": "model-x"}})
    assert config.shadow_agent.enabled is True
    assert config.shadow_agent.provider.base_url == "https://example.invalid"
    assert config.shadow_agent.provider.model == "model-x"
    with pytest.raises(ValueError, match="shadow_agent.tools"):
        AppConfig.from_mapping({"tools": {"enabled": True}})


def test_shadow_ledger_projection_is_untrusted_and_history_is_settled_only():
    ledger = SessionLedger()
    ledger.commit_input("turn-1", "用户问题")
    ledger.commit_delivery("speech-1", "已播报", type("Delivery", (), {"heard_text": "已播报", "termination": "completed", "uncertain_text": "", "unheard_text": "", "precision": "exact"})())
    ledger.commit_shadow_task_event(ShadowTaskEvent(1, "completed", "外部资料文本"))
    assert ledger.settled_history() == [{"role": "user", "content": "用户问题"}, {"role": "assistant", "content": "已播报"}]
    messages = ledger.context_messages("")
    assert "不可信数据" in messages[-1]["content"]


def test_shadow_package_does_not_depend_on_voice_runtime():
    root = Path(__file__).resolve().parents[1]
    contents = [item.read_text(encoding="utf-8") for item in (root / "voice_terminal" / "shadow_agent").glob("*.py")]
    assert all("controller." not in content and "server.websocket" not in content and "AgentEpisodeRuntime" not in content
               for content in contents)


def test_request_requires_contiguous_ids_and_replays_terminal(tmp_path):
    asyncio.run(_request_requires_contiguous_ids_and_replays_terminal(tmp_path))


async def _request_requires_contiguous_ids_and_replays_terminal(tmp_path):
    provider = ScriptedProvider([message("完成了")])
    manager = make_manager(tmp_path, provider)
    rejected = await manager.handle({"id": 2, "state": "request", "text": "x"})
    assert not rejected.accepted
    accepted = await manager.handle({"id": 1, "state": "request", "text": "x"}, [{"role": "user", "content": "hello"}])
    assert accepted.accepted
    await wait_terminal(manager, 1)
    first = manager.drain_events()
    assert first == [ShadowTaskEvent(1, "completed", "完成了")]
    replay = await manager.handle({"id": 1, "state": "request", "text": ""})
    assert replay.accepted and replay.status == "completed"
    assert manager.drain_events() == first


def test_native_function_output_is_retained_before_final_answer(tmp_path):
    asyncio.run(_native_function_output_is_retained_before_final_answer(tmp_path))


async def _native_function_output_is_retained_before_final_answer(tmp_path):
    (tmp_path / "note.md").write_text("facts", encoding="utf-8")
    provider = ScriptedProvider([function_call(), message("找到了 note.md")])
    manager = make_manager(tmp_path, provider)
    await manager.handle({"id": 1, "state": "request", "text": "查找笔记"})
    await wait_terminal(manager, 1)
    assert manager.drain_events() == [ShadowTaskEvent(1, "completed", "找到了 note.md")]
    assert len(provider.calls) == 2
    second_input = provider.calls[1]["input_items"]
    assert any(item.get("type") == "function_call" for item in second_input)
    assert any(item.get("type") == "function_call_output" for item in second_input)


def test_safe_point_checkpoint_runs_after_tool_result(tmp_path):
    asyncio.run(_safe_point_checkpoint_runs_after_tool_result(tmp_path))


async def _safe_point_checkpoint_runs_after_tool_result(tmp_path):
    clock = Clock()
    checkpoint = message('{"completed":false,"text":"已找到候选文件，正在整理。"}')
    provider = ScriptedProvider([function_call(), checkpoint, message("最终结论")], clock=clock, advance_on_call=5)
    manager = make_manager(tmp_path, provider, clock=clock)
    await manager.handle({"id": 1, "state": "request", "text": "查找"})
    await wait_terminal(manager, 1)
    events = manager.drain_events()
    assert events[-1] == ShadowTaskEvent(1, "completed", "最终结论")
    assert any(call["tools"] == () for call in provider.calls)


def test_modify_cancels_old_revision_and_keeps_new_result(tmp_path):
    asyncio.run(_modify_cancels_old_revision_and_keeps_new_result(tmp_path))


async def _modify_cancels_old_revision_and_keeps_new_result(tmp_path):
    first_started = asyncio.Event()

    async def blocked(_kwargs):
        first_started.set()
        await asyncio.Event().wait()

    provider = ScriptedProvider([blocked, message("按新方向完成")])
    manager = make_manager(tmp_path, provider)
    await manager.handle({"id": 1, "state": "request", "text": "旧方向"})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    receipt = await manager.handle({"id": 1, "state": "modify", "text": "只看本地文件"})
    assert receipt.accepted
    await wait_terminal(manager, 1)
    snapshot = manager.snapshot(1)
    assert snapshot["revision"] == 1
    assert snapshot["modifications"] == ["只看本地文件"]
    assert manager.drain_events() == [ShadowTaskEvent(1, "completed", "按新方向完成")]


def test_cancel_and_event_buffer_terminal_priority(tmp_path):
    asyncio.run(_cancel_and_event_buffer_terminal_priority(tmp_path))


async def _cancel_and_event_buffer_terminal_priority(tmp_path):
    first_started = asyncio.Event()

    async def blocked(_kwargs):
        first_started.set()
        await asyncio.Event().wait()

    manager = make_manager(tmp_path, ScriptedProvider([blocked]))
    await manager.handle({"id": 1, "state": "request", "text": "等待"})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert (await manager.handle({"id": 1, "state": "cancel", "text": ""})).accepted
    assert manager.drain_events() == [ShadowTaskEvent(1, "cancelled", "任务已取消。")]

    buffer = ShadowTaskEventBuffer()
    buffer.publish(ShadowTaskEvent(1, "running", "旧进度"))
    buffer.publish(ShadowTaskEvent(2, "running", "第二项"))
    buffer.publish(ShadowTaskEvent(1, "completed", "完成"))
    buffer.publish(ShadowTaskEvent(1, "running", "迟到进度"))
    assert buffer.drain() == [ShadowTaskEvent(1, "completed", "完成"), ShadowTaskEvent(2, "running", "第二项")]


def test_active_request_forces_checkpoint_without_duplicate_runner(tmp_path):
    asyncio.run(_active_request_forces_checkpoint_without_duplicate_runner(tmp_path))


async def _active_request_forces_checkpoint_without_duplicate_runner(tmp_path):
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def work(_kwargs):
        first_started.set()
        await release.wait()
        return function_call()

    provider = ScriptedProvider([work, message('{"completed":false,"text":"正在处理。"}'), message("完成")])
    manager = make_manager(tmp_path, provider)
    await manager.handle({"id": 1, "state": "request", "text": "任务"})
    await asyncio.wait_for(first_started.wait(), timeout=1)
    query = await manager.handle({"id": 1, "state": "request", "text": ""})
    assert query.accepted
    release.set()
    await wait_terminal(manager, 1)
    assert len(provider.calls) == 3
    assert manager.drain_events()[-1].status == "completed"


def test_ninth_task_is_rejected_without_consuming_id(tmp_path):
    asyncio.run(_ninth_task_is_rejected_without_consuming_id(tmp_path))


async def _ninth_task_is_rejected_without_consuming_id(tmp_path):
    started = asyncio.Event()

    async def blocked(_kwargs):
        started.set()
        await asyncio.Event().wait()

    manager = make_manager(tmp_path, ScriptedProvider([blocked] * 8))
    for identifier in range(1, 9):
        assert (await manager.handle({"id": identifier, "state": "request", "text": f"任务{identifier}"})).accepted
    await asyncio.wait_for(started.wait(), timeout=1)
    ninth = await manager.handle({"id": 9, "state": "request", "text": "任务9"})
    assert not ninth.accepted and ninth.status == "capacity"
    assert manager._last_accepted_id == 8
    await manager.close()


def test_deadline_runs_bounded_terminal_report(tmp_path):
    asyncio.run(_deadline_runs_bounded_terminal_report(tmp_path))


async def _deadline_runs_bounded_terminal_report(tmp_path):
    clock = Clock()
    provider = ScriptedProvider(
        [function_call(), message('{"completed":false,"text":"已读取部分资料"}')],
        clock=clock, advance_on_call=31,
    )
    manager = make_manager(tmp_path, provider, clock=clock)
    await manager.handle({"id": 1, "state": "request", "text": "测试截止"})
    await wait_terminal(manager, 1)
    event = manager.drain_events()[-1]
    assert event.status == "timed_out"
    assert "已读取部分资料" in event.text
