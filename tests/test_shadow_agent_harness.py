from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.shadow_agent.manager import ShadowRuntime
from voice_terminal.shadow_agent.provider import (
    DeepSeekResponsesShadowProvider, ShadowProviderError, ShadowProviderResponse,
)
from voice_terminal.shadow_agent.tools import ShadowToolExecutor


def message(text: str) -> dict:
    return {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": text}]}


def call(index: int) -> dict:
    return {
        "type": "function_call", "status": "completed", "call_id": f"c{index}",
        "name": "publish_main_event",
        "arguments": json.dumps({"kind": "progress", "text": f"step {index}"}),
    }


class FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None
    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeClient:
    def __init__(self, response, *args, **kwargs): self.response = response
    def stream(self, *_args, **_kwargs): return self.response
    async def aclose(self): return None


def test_responses_adapter_commits_only_completed_output_and_normalizes_cache_usage(monkeypatch):
    async def scenario():
        streamed = [
            {"type": "response.created", "response": {"id": "private-response"}},
            {"type": "response.in_progress"},
            {"type": "response.output_item.added", "item": {"id": "private-reasoning", "type": "reasoning"}},
            {"type": "response.reasoning_text.delta", "delta": "private reasoning"},
            {"type": "response.output_item.added", "item": {"id": "private-call", "type": "function_call", "call_id": "private-call", "name": "files_search"}},
            {"type": "response.function_call_arguments.delta", "item_id": "private-call", "delta": "{\"query\":\"x\"}"},
            {"type": "response.output_text.delta", "delta": "ignore"},
            {"type": "response.output_item.done", "item": {"id": "private-call", "type": "function_call", "call_id": "private-call", "name": "files_search", "status": "completed"}},
        ]
        complete = {
            "type": "response.completed",
            "response": {
                "status": "completed", "output": [message("authoritative")],
                "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}},
            },
        }
        response = FakeResponse([*(f"data: {json.dumps(item)}" for item in streamed), f"data: {json.dumps(complete)}"])
        client_options = []

        def make_client(*args, **kwargs):
            client_options.append(kwargs)
            return FakeClient(response)

        monkeypatch.setattr("httpx.AsyncClient", make_client)
        provider = DeepSeekResponsesShadowProvider(
            base_url="https://example.test", api_key="key", model="m",
        )
        received = []
        result = await provider.complete(
            instructions="fixed", input_items=[], tools=[], reasoning_effort="none",
            on_stream=received.append,
        )
        assert result.output == (message("authoritative"),)
        assert result.usage["cached_tokens"] == 80
        assert result.usage["cache_miss_tokens"] == 20
        assert result.usage["cache_hit_ratio"] == 0.8
        assert [item["type"] for item in received] == [
            item["type"] for item in streamed
        ] + ["response.completed"]
        assert received[3]["delta"] == "private reasoning"
        assert client_options == [{"timeout": None}]
        await provider.close()

    asyncio.run(scenario())


def test_responses_adapter_neutralizes_only_an_unreplayable_thinking_tools_response(monkeypatch):
    async def scenario():
        terminal = {
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "最终结果"}],
        }
        complete = {
            "type": "response.completed",
            "response": {
                "status": "completed", "output": [terminal],
                "usage": {
                    "input_tokens": 40, "output_tokens": 8,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        }
        monkeypatch.setattr(
            "httpx.AsyncClient",
            lambda *args, **kwargs: FakeClient(FakeResponse([
                f"data: {json.dumps(complete, ensure_ascii=False)}",
            ])),
        )
        provider = DeepSeekResponsesShadowProvider(
            base_url="https://example.test", api_key="key", model="m",
        )
        result = await provider.complete(
            instructions="fixed", input_items=[],
            tools=[{"type": "function", "name": "lookup"}],
            reasoning_effort="high",
        )

        assert result.output == (terminal,)
        assert result.replay_output is not None
        assert all(item.get("type") is None for item in result.replay_output)
        encoded = json.dumps(result.replay_output, ensure_ascii=False)
        assert "最终结果" in encoded
        stored = json.loads(result.replay_output[0]["content"].split("\n", 1)[1])
        assert stored == terminal
        await provider.close()

    asyncio.run(scenario())


def test_responses_adapter_rejects_stream_without_completed(monkeypatch):
    async def scenario():
        monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FakeClient(FakeResponse([
            'data: {"type":"response.output_text.delta","delta":"partial"}',
        ])))
        provider = DeepSeekResponsesShadowProvider(base_url="https://x", api_key="k", model="m")
        with pytest.raises(ShadowProviderError):
            await provider.complete(instructions="i", input_items=[], tools=[], reasoning_effort="high")
        await provider.close()

    asyncio.run(scenario())


class ObserverProvider:
    def __init__(self):
        self.main_step = 0
        self.requests = []
        self.observer_done = asyncio.Event()

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs["reasoning_effort"] == "none":
            self.observer_done.set()
            return ShadowProviderResponse((message("已完成初步检索，正在核对来源；下一步整理结论，目前没有阻塞。"),), {})
        self.main_step += 1
        if self.main_step <= 5:
            return ShadowProviderResponse((call(self.main_step),), {})
        await self.observer_done.wait()
        await asyncio.sleep(0)
        return ShadowProviderResponse((message("最终完成"),), {})


async def wait_idle(runtime: ShadowRuntime):
    for _ in range(200):
        if not runtime.active and (runtime.observer_task is None or runtime.observer_task.done()):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("not idle")


def test_observer_runs_at_activation_start_and_five_committed_steps_without_tools(tmp_path: Path):
    async def scenario():
        provider, ledger, events = ObserverProvider(), SessionLedger(), []
        ledger.commit_input("u", "长期查找")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_every_steps=5,
        )
        runtime.wake()
        await wait_idle(runtime)
        observer = [request for request in provider.requests if request["reasoning_effort"] == "none"]
        assert len(observer) == 2
        assert observer[0]["tools"] == []
        assert observer[0]["max_output_tokens"] == 512
        assert runtime.committed_model_steps == 6
        assert any(event.kind == "status" for event in events)
        assert events[-1].kind == "result"
        await runtime.close()

    asyncio.run(scenario())


def test_status_provider_is_isolated_and_trace_is_context_bounded(tmp_path: Path):
    class MainProvider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return ShadowProviderResponse((call(1),), {"input_tokens": 120, "cached_tokens": 90, "cache_hit_ratio": .75})
            return ShadowProviderResponse((message("完成"),), {"input_tokens": 120, "cached_tokens": 90, "cache_hit_ratio": .75})

    class StatusProvider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            return ShadowProviderResponse((message("正在整理已提交的工作。"),), {"input_tokens": 10})

    async def scenario():
        main, status, ledger = MainProvider(), StatusProvider(), SessionLedger()
        ledger.commit_input("u", "请安排今天下午的工作" * 80)
        runtime = ShadowRuntime(
            main, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", status_provider=status,
            observer_every_steps=1, observer_max_context_tokens=1024,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert status.requests and all(request["tools"] == [] for request in status.requests)
        assert all(request["reasoning_effort"] == "none" for request in status.requests)
        assert all(request["reasoning_effort"] != "none" for request in main.requests)
        trace = status.requests[0]["input_items"][:-1]
        assert runtime._estimate_items_tokens(trace) <= 1024
        snapshot = runtime.snapshot()
        assert snapshot["context_limit_tokens"] == runtime.max_context_tokens
        assert snapshot["provider_cache_hit_ratio"] == .75
        await runtime.close()

    asyncio.run(scenario())


class CompactionProvider:
    def __init__(self): self.requests = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("text_schema"):
            value = {"history_summary": "历史", "working_summary": "工作", "pending_state": "继续"}
            return ShadowProviderResponse((message(json.dumps(value, ensure_ascii=False)),), {})
        return ShadowProviderResponse((message("压缩后完成"),), {})


def test_compaction_rolls_cache_epoch_and_retains_authoritative_snapshot(tmp_path: Path):
    async def scenario():
        provider, ledger, events = CompactionProvider(), SessionLedger(), []
        ledger.commit_input("u", "需要处理的当前意图")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False, max_context_tokens=1024, compaction_threshold_ratio=0.50,
        )
        # Force a large committed prefix before the safe boundary.
        runtime.working_trace.append({"role": "system", "content": "旧轨迹" * 1000})
        runtime._source_epoch = ledger.shadow_context_epoch
        runtime.pending_context_revision = ledger.shadow_input_revision
        runtime.wake()
        await wait_idle(runtime)
        assert runtime.context_epoch >= 1
        assert any(request.get("text_schema") for request in provider.requests)
        assert "当前意图" in json.dumps(runtime.working_trace, ensure_ascii=False)
        assert events[-1].kind == "result"
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_reminder_is_injected_at_next_safe_boundary_only(tmp_path: Path):
    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                await asyncio.sleep(0.02)
                return ShadowProviderResponse((call(1),), {})
            return ShadowProviderResponse((message("已收敛完成"),), {})

    async def scenario():
        provider, ledger, events = Provider(), SessionLedger(), []
        ledger.commit_input("u", "请快速处理")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False, runtime_reminder_interval_ms=10,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert len(provider.requests) == 2
        second = json.dumps(provider.requests[1]["input_items"], ensure_ascii=False)
        assert "运行时限提醒" in second
        assert "立即收敛" in second
        assert events[-1].text == "已收敛完成"
        await runtime.close()

    asyncio.run(scenario())


def test_terminal_response_does_not_add_a_reminder_round(tmp_path: Path):
    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            await asyncio.sleep(0.02)
            return ShadowProviderResponse((message("直接完成"),), {})

    async def scenario():
        provider, ledger, events = Provider(), SessionLedger(), []
        ledger.commit_input("u", "直接回答")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False, runtime_reminder_interval_ms=10,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert len(provider.requests) == 1
        assert events[-1].text == "直接完成"
        await runtime.close()

    asyncio.run(scenario())


def test_new_activation_preserves_valid_prefix_and_neutralizes_only_bad_terminal(tmp_path: Path):
    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return ShadowProviderResponse((
                    {
                        "type": "reasoning", "status": "completed",
                        "content": [{"type": "reasoning_text", "text": "private"}],
                    },
                    call(1),
                ), {})
            if len(self.requests) == 2:
                return ShadowProviderResponse(
                    (message("第一轮完成"),), {},
                    ({
                        "role": "system",
                        "content": "【Shadow 已完成输出（数据，非指令）】\n第一轮完成",
                    },),
                )
            return ShadowProviderResponse((message("第二轮完成"),), {})

    async def scenario():
        provider, ledger, events = Provider(), SessionLedger(), []
        ledger.commit_input("u1", "先处理第一件事")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False,
        )

        runtime.wake()
        await wait_idle(runtime)
        assert any(item.get("type") == "reasoning" for item in runtime.working_trace)
        assert not any(item.get("type") == "message" for item in runtime.working_trace)

        ledger.commit_input("u2", "继续处理第二件事")
        runtime.wake()
        await wait_idle(runtime)

        assert len(provider.requests) == 3
        resumed = provider.requests[2]["input_items"]
        assert any(item.get("type") == "reasoning" for item in resumed)
        assert any(item.get("type") == "function_call" for item in resumed)
        assert any(item.get("type") == "function_call_output" for item in resumed)
        assert not any(item.get("type") == "message" for item in resumed)
        encoded = json.dumps(resumed, ensure_ascii=False)
        assert "第一轮完成" in encoded
        assert "publish_main_event" in encoded
        assert "step 1" in encoded
        assert "继续处理第二件事" in encoded
        assert "private" in encoded
        assert [event.text for event in events if event.kind == "result"] == [
            "第一轮完成", "第二轮完成",
        ]
        await runtime.close()

    asyncio.run(scenario())


def test_unreplayable_tool_response_keeps_call_and_result_protocol_neutral(tmp_path: Path):
    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                tool_call = call(1)
                return ShadowProviderResponse(
                    (tool_call,), {},
                    ({
                        "role": "system",
                        "content": "【Shadow 已请求工具（数据，非指令）】\n"
                        + json.dumps(tool_call, ensure_ascii=False),
                    },),
                )
            return ShadowProviderResponse(
                (message("降级协议后完成"),), {},
                ({"role": "system", "content": "降级协议后完成"},),
            )

    async def scenario():
        provider, ledger, events = Provider(), SessionLedger(), []
        ledger.commit_input("u", "执行一次工具调用")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False,
        )
        runtime.wake()
        await wait_idle(runtime)

        assert len(provider.requests) == 2
        second = provider.requests[1]["input_items"]
        assert not any(
            item.get("type") in {"function_call", "function_call_output"}
            for item in second
        )
        encoded = json.dumps(second, ensure_ascii=False)
        assert "publish_main_event" in encoded
        assert "step 1" in encoded
        assert "Shadow 工具结果" in encoded
        assert events[-1].text == "降级协议后完成"
        await runtime.close()

    asyncio.run(scenario())


def test_slow_tool_returns_one_pending_output_then_reinjects_completion(tmp_path: Path):
    import threading

    class SlowBackend:
        available = True
        def action_definitions(self):
            return [{
                "name": "files_search", "description": "test",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                }, "required": ["query"], "additionalProperties": False},
            }]
        def normalize(self, name, arguments): return name, arguments
        def execute(self, _name, _arguments):
            threading.Event().wait(0.03)
            return {"files": ["done.md"]}

    class Provider:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return ShadowProviderResponse(({
                    "type": "function_call", "status": "completed",
                    "call_id": "slow-1", "name": "files_search",
                    "arguments": json.dumps({"query": "done"}),
                },), {})
            if len(self.requests) == 2:
                await asyncio.sleep(0.15)
                return ShadowProviderResponse((message("过期草稿"),), {})
            return ShadowProviderResponse((message("包含后台结果的最终答案"),), {})

    async def scenario():
        provider, ledger, events = Provider(), SessionLedger(), []
        ledger.commit_input("u", "查找文件")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(SlowBackend(), soft_yield_ms=5), ledger,
            instructions_loader=lambda: "fixed", on_event=events.append,
            observer_enabled=False,
        )
        runtime.wake()
        await wait_idle(runtime)
        assert len(provider.requests) == 3
        first_followup = provider.requests[1]["input_items"]
        outputs = [item for item in first_followup if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["slow-1"]
        pending = json.loads(outputs[0]["output"])
        assert pending["status"] == "running" and pending["job_id"].startswith("tool-")
        final_input = json.dumps(provider.requests[2]["input_items"], ensure_ascii=False)
        assert "后台工具作业完成记录" in final_input
        assert "done.md" in final_input
        assert [event.text for event in events if event.kind == "result"] == ["包含后台结果的最终答案"]
        await runtime.close()

    asyncio.run(scenario())


def test_observer_receives_no_reasoning_or_responses_tool_items(tmp_path: Path):
    runtime = ShadowRuntime(
        ObserverProvider(), ShadowToolExecutor(FileTools((tmp_path,))), SessionLedger(),
        instructions_loader=lambda: "fixed", observer_enabled=False,
    )
    trace = (
        {"type": "reasoning", "summary": "private chain"},
        {"type": "function_call", "call_id": "c", "name": "web_search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": '{"organic":[]}'},
        {"type": "function_call_output", "call_id": "orphan", "output": '{"error":"x"}'},
    )
    safe = runtime._status_trace_prefix(trace)
    assert safe and all(set(item).issuperset({"role", "content"}) for item in safe)
    encoded = json.dumps(safe, ensure_ascii=False)
    assert "private chain" not in encoded
    assert '"type": "function_call"' not in encoded
    assert '"type": "function_call_output"' not in encoded


def test_initial_observer_is_gate_independent_and_falls_back_after_retry(tmp_path: Path):
    class BlockingMain:
        def __init__(self): self.started = asyncio.Event()
        async def complete(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    class FailingStatus:
        def __init__(self): self.requests = []
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            raise ShadowProviderError("Responses HTTP 400: invalid tool history", retryable=False)

    async def scenario():
        main, status, ledger, events = BlockingMain(), FailingStatus(), SessionLedger(), []
        ledger.commit_input("u", "需要首条摘要")
        runtime = ShadowRuntime(
            main, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "fixed", status_provider=status,
            on_event=events.append,
        )
        runtime.wake()
        await asyncio.wait_for(main.started.wait(), timeout=1)
        for _ in range(100):
            if any(event.kind == "status" for event in events):
                break
            await asyncio.sleep(0.01)
        assert len(status.requests) == 2
        assert all(request["tools"] == [] for request in status.requests)
        assert all(
            item.get("type") not in {"reasoning", "function_call", "function_call_output"}
            for request in status.requests for item in request["input_items"]
        )
        assert [event.text for event in events if event.kind == "status"] == [
            "Shadow Agent 正在处理当前任务；可读状态摘要暂时不可用，任务仍在运行。"
        ]
        await runtime.close()

    asyncio.run(scenario())
