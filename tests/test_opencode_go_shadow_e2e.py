"""Long-loop acceptance coverage for the single Shadow runtime."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from voice_terminal.agent.tool_registry import FileTools
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.shadow_agent import ShadowRuntime, ShadowToolExecutor
from voice_terminal.shadow_agent.provider import ShadowProviderResponse


def message(text: str) -> dict:
    return {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": text}]}


class LongProvider:
    def __init__(self):
        self.main_steps = 0
        self.main_inputs = []

    async def complete(self, **kwargs):
        if kwargs["reasoning_effort"] == "none":
            return ShadowProviderResponse((message("正在持续核对已提交资料，当前无阻塞。"),), {})
        self.main_inputs.append(tuple(kwargs["input_items"]))
        self.main_steps += 1
        if self.main_steps <= 20:
            return ShadowProviderResponse(({
                "type": "function_call", "status": "completed", "call_id": f"c{self.main_steps}",
                "name": "publish_main_event",
                "arguments": json.dumps({"kind": "progress", "text": f"第 {self.main_steps} 步"}, ensure_ascii=False),
            },), {"input_tokens": self.main_steps * 20, "cached_tokens": max(0, self.main_steps - 1) * 20})
        return ShadowProviderResponse((message("二十轮以上循环完成"),), {})


def test_opencode_go_shadow_agent_20_plus_round_acceptance_loop(tmp_path: Path):
    async def scenario():
        provider, ledger, events = LongProvider(), SessionLedger(), []
        ledger.commit_input("u1", "进行长期只读核对")
        runtime = ShadowRuntime(
            provider, ShadowToolExecutor(FileTools((tmp_path,))), ledger,
            instructions_loader=lambda: "stable opencode shadow prompt", on_event=events.append,
            observer_every_steps=5,
        )
        assert runtime.wake() == "started"
        first_runner = runtime.runner
        assert runtime.wake() == "coalesced"
        await first_runner
        if runtime.observer_task is not None:
            await asyncio.gather(runtime.observer_task, return_exceptions=True)
        assert provider.main_steps == 21
        assert runtime.committed_model_steps == 21
        assert not runtime.active and runtime.runner is first_runner
        assert [event.text for event in events if event.kind == "result"] == ["二十轮以上循环完成"]
        assert all("task_id" not in json.dumps(items, ensure_ascii=False) for items in provider.main_inputs)
        # Every subsequent request contains the previous committed sequence as
        # an exact item prefix; only response/output items are appended.
        for previous, current in zip(provider.main_inputs, provider.main_inputs[1:]):
            assert current[:len(previous)] == previous
        await runtime.close()

    asyncio.run(scenario())
