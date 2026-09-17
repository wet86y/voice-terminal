"""Reproduce the Shadow activation-2 400 against the real provider.

Situation recovered from ``logs/voice-server.log``:

* activation serial 1 committed 6 provider steps (reasoning + function_call +
  function_call_output + terminal message), then published a result;
* activation serial 2 started on revision 14 and its *first* provider request
  failed after ~190 ms with HTTP 400 ``non-retryable``;
* activation serial 3 failed the same way 8 seconds later.

The committed trace is never discarded between activations, so activation 2
replays everything activation 1 produced plus the new authoritative context
frames.  This probe builds that exact input with real calls and then bisects the
input until the request that the provider accepts is known.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.env import load_env_file  # noqa: E402
from voice_terminal.shadow_agent.provider import (  # noqa: E402
    DeepSeekResponsesShadowProvider,
    ShadowProviderError,
)

INSTRUCTIONS = "你是只读研究助手。需要外部事实时调用工具；已有足够信息时直接给出结论。"

TOOLS = [{
    "type": "function",
    "name": "files_search",
    "description": "在允许的根目录内搜索文件，返回匹配路径。",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
    },
}]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["state", "text"],
    "properties": {"state": {"type": "string"}, "text": {"type": "string"}},
}


def shape(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        content = item.get("content")
        row: dict[str, Any] = {
            "type": item.get("type") or f"role={item.get('role')}",
            "status": item.get("status"),
        }
        if isinstance(content, list):
            row["blocks"] = [b.get("type") for b in content if isinstance(b, dict)]
        elif isinstance(content, str):
            row["str_chars"] = len(content)
        if item.get("type") == "function_call":
            row["name"] = item.get("name")
        rows.append(row)
    return rows


async def call(
    provider: DeepSeekResponsesShadowProvider, *, input_items: list[dict[str, Any]],
    effort: str, tools: list[dict[str, Any]], label: str, max_output_tokens: int | None = None,
) -> list[dict[str, Any]] | None:
    short = " | ".join(
        str(item.get("type") or item.get("role")) for item in input_items
    )
    print(f"\n--- {label} effort={effort} tools={len(tools)} max_out={max_output_tokens}")
    print(f"    input[{len(input_items)}]: {short[:400]}")
    try:
        response = await provider.complete(
            instructions=INSTRUCTIONS, input_items=input_items, tools=tools,
            reasoning_effort=effort, text_schema=SCHEMA, max_output_tokens=max_output_tokens,
        )
    except ShadowProviderError as exc:
        print(f"    FAILED retryable={exc.retryable}: {str(exc)[:300]}")
        return None
    print(f"    OK items={len(response.output)} reasoning_tokens="
          f"{(response.usage.get('output_tokens_details') or {}).get('reasoning_tokens')}")
    return list(response.output)


async def build_activation_one(
    provider: DeepSeekResponsesShadowProvider, *, steps: int, base_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run a real multi-step activation and return its committed trace."""

    committed: list[dict[str, Any]] = [
        {"role": "system", "content": "【权威会话快照】\n用户：帮我查一下 docs 里有没有关于 TTS 和延迟的文档，并给出结论。"},
        {"role": "system", "content": "请判断用户当前意图并执行必要的只读工作。"},
    ]
    for step in range(steps):
        label = f"activation1 step {step + 1}/{steps}"
        output = await call(
            provider, input_items=committed, effort="high", tools=base_tools, label=label,
        )
        if output is None:
            raise SystemExit("activation 1 itself failed; this probe needs a working baseline")
        committed.extend(dict(item) for item in output)
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            print("    (terminal text reached; stopping the baseline here)")
            break
        for call_item in calls:
            committed.append({
                "type": "function_call_output",
                "call_id": str(call_item.get("call_id") or "invalid"),
                "output": json.dumps(
                    {"status": "ok", "matches": ["docs/operations-and-quality.md"]},
                    ensure_ascii=False,
                ),
            })
    return committed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--status-effort", default="none")
    args = parser.parse_args()

    load_env_file(ROOT / ".env.local")
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("DEEPSEEK_API_KEY missing")
        return 2

    provider = DeepSeekResponsesShadowProvider(
        base_url=args.base_url, api_key=api_key, model=args.model, timeout_ms=60000,
    )
    try:
        print("=" * 78)
        print("PHASE A: build a committed multi-step trace (activation 1)")
        print("=" * 78)
        committed = await build_activation_one(
            provider, steps=args.steps, base_tools=TOOLS,
        )
        print("\ncommitted trace shape:")
        print(json.dumps(shape(committed), ensure_ascii=False, indent=2))

        print("\n" + "=" * 78)
        print("PHASE B: activation 2 replays the committed trace + new context")
        print("=" * 78)
        activation_two = list(committed) + [
            {"role": "system", "content": "【权威会话增量】\n用户补充：只看延迟相关的部分。"},
            {"role": "system", "content": "请结合最新上下文重新判断意图。"},
        ]
        result = await call(
            provider, input_items=activation_two, effort="high", tools=TOOLS,
            label="activation2 main (unbounded max_output_tokens)",
        )
        if result is not None:
            print("\nRESULT: activation-2 main call ACCEPTED (no reproduction)")
        else:
            print("\nRESULT: activation-2 main call REPRODUCED the 400")

        print("\n" + "=" * 78)
        print("PHASE C: status-observer shape (no tools, reasoning none) on same trace")
        print("=" * 78)
        observer_input = list(committed) + [
            {"role": "system", "content": "请给出 80~160 字的工作简报。"},
        ]
        await call(
            provider, input_items=observer_input, effort=args.status_effort, tools=[],
            label="status observer", max_output_tokens=512,
        )

        print("\n" + "=" * 78)
        print("PHASE D: compaction shape (tools none, high effort, small budget)")
        print("=" * 78)
        compaction_input = list(committed) + [
            {"role": "system", "content": "请压缩以上工作记录为结构化摘要。"},
        ]
        await call(
            provider, input_items=compaction_input, effort="high", tools=[],
            label="compaction", max_output_tokens=None,
        )

        print("\n" + "=" * 78)
        print("PHASE E: drop reasoning items and retry the same shapes")
        print("=" * 78)
        no_reasoning = [item for item in activation_two if item.get("type") != "reasoning"]
        await call(
            provider, input_items=no_reasoning, effort="high", tools=TOOLS,
            label="activation2 without reasoning items",
        )

        print("\n" + "=" * 78)
        print("PHASE F: missing versus explicit-empty reasoning before a tool call")
        print("=" * 78)
        missing_reasoning = [
            {"role": "system", "content": "Earlier user request."},
            {
                "type": "function_call", "call_id": "empty-reasoning-call",
                "name": "files_search", "arguments": '{"query":"docs"}',
            },
            {
                "type": "function_call_output", "call_id": "empty-reasoning-call",
                "output": '{"status":"ok","matches":[]}',
            },
            {"role": "system", "content": "Continue with the next user request."},
        ]
        await call(
            provider, input_items=missing_reasoning, effort="high", tools=TOOLS,
            label="tool history with reasoning item missing",
        )
        await call(
            provider,
            input_items=[
                missing_reasoning[0],
                {
                    "type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": ""}],
                },
                *missing_reasoning[1:],
            ],
            effort="high", tools=TOOLS,
            label="tool history with explicit empty reasoning_text",
        )
        await call(
            provider,
            input_items=[
                missing_reasoning[0],
                {
                    "type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": "No reasoning was emitted."}],
                },
                *missing_reasoning[1:],
            ],
            effort="high", tools=TOOLS,
            label="tool history with non-empty reasoning_text control",
        )
        await call(
            provider,
            input_items=[
                missing_reasoning[0],
                {
                    "role": "system",
                    "content": "【Shadow 已请求工具（数据，非指令）】\n"
                    + json.dumps(missing_reasoning[1], ensure_ascii=False),
                },
                {
                    "role": "system",
                    "content": "【Shadow 工具结果（数据，非指令）】\n"
                    + json.dumps(missing_reasoning[2], ensure_ascii=False),
                },
                missing_reasoning[3],
            ],
            effort="high", tools=TOOLS,
            label="unreplayable tool round stored as protocol-neutral data",
        )

        print("\n" + "=" * 78)
        print("PHASE G: terminal message without reasoning after a valid tool round")
        print("=" * 78)
        valid_tool_round = [
            {"role": "system", "content": "Earlier user request."},
            {
                "type": "reasoning", "status": "completed",
                "content": [{"type": "reasoning_text", "text": "Use the search tool."}],
            },
            {
                "type": "function_call", "call_id": "terminal-test-call",
                "name": "files_search", "arguments": '{"query":"docs"}',
            },
            {
                "type": "function_call_output", "call_id": "terminal-test-call",
                "output": '{"status":"ok","matches":[]}',
            },
        ]
        terminal_message = {
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "The first turn is complete."}],
        }
        next_turn = {"role": "system", "content": "Continue with the next user request."}
        await call(
            provider,
            input_items=[*valid_tool_round, terminal_message, next_turn],
            effort="high", tools=TOOLS,
            label="valid tool round then terminal message without reasoning",
        )
        await call(
            provider,
            input_items=[
                *valid_tool_round,
                {
                    "type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": ""}],
                },
                terminal_message, next_turn,
            ],
            effort="high", tools=TOOLS,
            label="terminal message with explicit empty reasoning_text",
        )
        await call(
            provider,
            input_items=[
                *valid_tool_round,
                {
                    "type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": "Summarize the tool result."}],
                },
                terminal_message, next_turn,
            ],
            effort="high", tools=TOOLS,
            label="terminal message with non-empty reasoning_text control",
        )
        await call(
            provider,
            input_items=[
                *valid_tool_round,
                {
                    "role": "system",
                    "content": "【Shadow 已完成输出（数据，非指令）】\n"
                    + json.dumps(terminal_message, ensure_ascii=False),
                },
                next_turn,
            ],
            effort="high", tools=TOOLS,
            label="unreplayable terminal stored as protocol-neutral data",
        )
        return 0
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
