from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from voice_terminal.shadow_agent.manager import ShadowTaskManager
from voice_terminal.shadow_agent.provider import DeepSeekResponsesShadowProvider, ShadowProviderError, ShadowProviderResponse
from voice_terminal.shadow_agent.tools import ShadowToolExecutor


def _message(text: str) -> ShadowProviderResponse:
    return ShadowProviderResponse(({
        "type": "message", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    },), {})


def _call(call_id: str, name: str, arguments: str) -> dict:
    return {"type": "function_call", "status": "completed", "call_id": call_id, "name": name, "arguments": arguments}


async def _wait_terminal(manager: ShadowTaskManager, identifier: int) -> dict:
    for _ in range(200):
        snapshot = manager.snapshot(identifier)
        if snapshot is not None and snapshot["status"] in {"completed", "cancelled", "timed_out", "failed"}:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("task did not reach a terminal state")


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        return await response(kwargs) if callable(response) else response


def test_responses_adapter_uses_completed_output_only_and_preserves_native_items(monkeypatch):
    async def scenario():
        captured: dict = {}

        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"type":"response.output_item.added","item":{"type":"message"}}'
                yield 'data: ' + json.dumps({"type": "response.completed", "response": {
                    "status": "completed", "usage": {"input_tokens": 3}, "output": [
                        {"type": "reasoning", "id": "rs_1", "status": "completed", "summary": []},
                        _call("fc_1", "files_search", '{"query":"note"}'),
                    ],
                }})

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, method, url, *, headers, json):
                captured.update(method=method, url=url, headers=headers, payload=json)
                return Stream()

        fake_httpx = SimpleNamespace(Timeout=lambda *args, **kwargs: (args, kwargs), AsyncClient=lambda **_kwargs: Client())
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
        provider = DeepSeekResponsesShadowProvider(
            base_url="https://example.invalid/v1/", api_key="secret", model="test-model",
            session_id="shadow-session",
        )
        result = await provider.complete(
            instructions="rules", input_items=[{"role": "user", "content": "find note"}],
            tools=[{"type": "function", "name": "files_search", "parameters": {}}], reasoning_effort="high",
        )
        assert captured["method"] == "POST"
        assert captured["url"] == "https://example.invalid/v1/responses"
        assert captured["payload"]["reasoning"] == {"effort": "high"}
        assert captured["payload"]["tool_choice"] == "auto"
        assert captured["headers"]["x-opencode-session"] == "shadow-session"
        assert result.output[0]["type"] == "reasoning"
        assert result.output[1]["call_id"] == "fc_1"
        assert result.usage == {"input_tokens": 3}

    asyncio.run(scenario())


def test_responses_adapter_rejects_incomplete_terminal(monkeypatch):
    async def scenario():
        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"type":"response.incomplete","response":{"error":{"code":"length"}}}'

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return Stream()

        monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Timeout=lambda *_a, **_k: None, AsyncClient=lambda **_k: Client()))
        provider = DeepSeekResponsesShadowProvider(base_url="https://example.invalid", api_key="key", model="model")
        with pytest.raises(ShadowProviderError, match="incomplete"):
            await provider.complete(instructions="x", input_items=[], tools=[], reasoning_effort="high")

    asyncio.run(scenario())


def test_native_multi_tool_roundtrip_keeps_call_order_and_outputs(tmp_path: Path):
    async def scenario():
        (tmp_path / "note.md").write_text("shadow facts", encoding="utf-8")
        first = ShadowProviderResponse((
            _call("search", "files_search", '{"query":"note"}'),
            _call("read", "files_read_text", '{"path":"note.md"}'),
        ), {})
        provider = _Provider([first, _message("已找到并读取：shadow facts")])
        manager = ShadowTaskManager(provider, ShadowToolExecutor(FileTools((tmp_path,))), instructions_loader=lambda: "rules")
        assert (await manager.handle({"id": 1, "state": "request", "text": "读取 note"})).accepted
        await _wait_terminal(manager, 1)
        second_input = provider.calls[1]["input_items"]
        outputs = [item for item in second_input if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["search", "read"]
        assert json.loads(outputs[0]["output"])["files"][0]["path"].endswith("note.md")
        assert json.loads(outputs[1]["output"])["text"] == "shadow facts"
        assert manager.drain_events()[-1].text == "已找到并读取：shadow facts"

    asyncio.run(scenario())


def test_readonly_executor_has_shared_two_slot_limit_and_mock_web_search():
    async def scenario():
        active = 0
        observed_max = 0
        lock = threading.Lock()

        class Backend:
            available = True
            service_status = "READY"

            def normalize(self, name, arguments):
                return name, arguments

            def execute(self, _name, _arguments):
                nonlocal active, observed_max
                with lock:
                    active += 1
                    observed_max = max(observed_max, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                return {"ok": True}

            def action_definitions(self):
                return []

        executor = ShadowToolExecutor(Backend(), max_concurrency=2)
        calls = [_call(str(index), "files_search", '{"query":"x"}') for index in range(4)]
        outputs = await executor.execute_calls(calls)
        assert observed_max == 2
        assert [item["call_id"] for item in outputs] == ["0", "1", "2", "3"]

        class SearchClient:
            api_key = "mock-key"

            def search(self, query):
                assert query == "current topic"
                return {"organic": [{"title": "mock"}], "related_searches": []}

        web = ShadowToolExecutor(None, WebSearchTools(SearchClient()))
        output = await web.execute_calls([_call("web", "web_search", '{"query":"current topic"}')])
        assert json.loads(output[0]["output"])["organic"][0]["title"] == "mock"

    asyncio.run(scenario())


def test_modify_invalidates_cancellation_suppressed_old_response_before_tools(tmp_path: Path):
    async def scenario():
        started = asyncio.Event()
        tool_calls = 0

        class Tools:
            def responses_tools(self):
                return []

            async def execute_calls(self, _calls):
                nonlocal tool_calls
                tool_calls += 1
                return []

            def close(self):
                pass

        async def stale_response(_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A non-cooperative transport may still yield a complete old
                # response after cancellation.  It must be discarded.
                return ShadowProviderResponse((_call("old", "files_search", '{"query":"old"}'),), {})

        provider = _Provider([stale_response, _message("新方向完成")])
        manager = ShadowTaskManager(provider, Tools(), instructions_loader=lambda: "rules")
        await manager.handle({"id": 1, "state": "request", "text": "旧方向"})
        await asyncio.wait_for(started.wait(), timeout=1)
        receipt = await manager.handle({"id": 1, "state": "modify", "text": "新方向"})
        assert receipt.accepted
        await _wait_terminal(manager, 1)
        assert tool_calls == 0
        assert manager.snapshot(1)["revision"] == 1
        assert manager.drain_events() == [manager.snapshot(1)["last_event"]]

    asyncio.run(scenario())


def test_cancel_invalidates_cancellation_suppressed_old_response_before_tools():
    async def scenario():
        started = asyncio.Event()
        tool_calls = 0

        class Tools:
            def responses_tools(self):
                return []

            async def execute_calls(self, _calls):
                nonlocal tool_calls
                tool_calls += 1
                return []

            def close(self):
                pass

        async def stale_response(_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ShadowProviderResponse((_call("old", "files_search", '{"query":"old"}'),), {})

        manager = ShadowTaskManager(_Provider([stale_response]), Tools(), instructions_loader=lambda: "rules")
        await manager.handle({"id": 1, "state": "request", "text": "旧任务"})
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (await manager.handle({"id": 1, "state": "cancel", "text": ""})).accepted
        assert tool_calls == 0
        assert manager.drain_events()[-1].status == "cancelled"

    asyncio.run(scenario())


def test_work_deadline_hard_cancels_inflight_work_then_reports_timeout():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_work(_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        provider = _Provider([
            blocking_work,
            _message('{"completed":false,"text":"已完成部分检索"}'),
        ])
        tools = ShadowToolExecutor(None)
        manager = ShadowTaskManager(
            provider, tools, instructions_loader=lambda: "rules", slice_seconds=1,
            work_seconds=0.03, terminal_report_seconds=0.2,
        )
        await manager.handle({"id": 1, "state": "request", "text": "有截止的任务"})
        await asyncio.wait_for(started.wait(), timeout=1)
        await _wait_terminal(manager, 1)
        assert cancelled.is_set()
        event = manager.drain_events()[-1]
        assert event.status == "timed_out"
        assert "已完成部分检索" in event.text

    asyncio.run(scenario())
