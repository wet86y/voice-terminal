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
        partial = {"type": "response.output_text.delta", "delta": "ignore"}
        complete = {
            "type": "response.completed",
            "response": {
                "status": "completed", "output": [message("authoritative")],
                "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}},
            },
        }
        response = FakeResponse([f"data: {json.dumps(partial)}", f"data: {json.dumps(complete)}"])
        monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FakeClient(response))
        provider = DeepSeekResponsesShadowProvider(
            base_url="https://example.test", api_key="key", model="m",
        )
        result = await provider.complete(
            instructions="fixed", input_items=[], tools=[], reasoning_effort="none",
        )
        assert result.output == (message("authoritative"),)
        assert result.usage["cached_tokens"] == 80
        assert result.usage["cache_miss_tokens"] == 20
        assert result.usage["cache_hit_ratio"] == 0.8
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


def test_observer_runs_exactly_at_five_committed_steps_without_tools(tmp_path: Path):
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
        assert len(observer) == 1
        assert observer[0]["tools"] == []
        assert observer[0]["max_output_tokens"] == 512
        assert runtime.committed_model_steps == 6
        assert any(event.kind == "status" for event in events)
        assert events[-1].kind == "result"
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
