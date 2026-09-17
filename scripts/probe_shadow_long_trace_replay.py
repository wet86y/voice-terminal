"""Replay a production-scale Shadow trace through the real provider.

``logs/voice-server.log`` shows activation serial 1 committing six model steps
with interleaved reasoning, function calls and tool outputs, and then activation
serial 2 failing its *first* provider request with HTTP 400
``The `reasoning_text` in the thinking mode must be passed back to the API``.

The runtime only ever appends, never filters, completed output items, so this
probe asks the remaining structural question: is a long production-shaped
committed prefix (many reasoning items, interleaved tool calls, system
continuation frames) itself unsendable, independently of how it was built?
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
    DeepSeekResponsesShadowProvider, ShadowProviderError,
)

INSTRUCTIONS = (
    "你是只读研究助手。需要外部事实时调用工具；已有足够信息时直接给出结论。"
)

TOOLS = [
    {
        "type": "function", "name": name, "strict": True,
        "description": description,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
    }
    for name, description in (
        ("files_search", "在允许的根目录内搜索文件。"),
        ("files_read_text", "读取允许根目录内的文本文件。"),
    )
]

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["state", "text"],
    "properties": {"state": {"type": "string"}, "text": {"type": "string"}},
}


def kinds(items: list[dict[str, Any]]) -> str:
    return " | ".join(str(item.get("type") or f"role={item.get('role')}") for item in items)


def reasoning_ok(items: list[dict[str, Any]]) -> tuple[int, int]:
    total = with_text = 0
    for item in items:
        if item.get("type") != "reasoning":
            continue
        total += 1
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "reasoning_text"
            and (block.get("text") or "").strip()
            for block in content
        ):
            with_text += 1
    return total, with_text


async def call(
    provider: DeepSeekResponsesShadowProvider, *, items: list[dict[str, Any]],
    label: str, effort: str = "high", tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    total, with_text = reasoning_ok(items)
    print(f"\n--- {label}: input={len(items)} reasoning={total} (with_text={with_text})")
    print(f"    {kinds(items)[:420]}")
    try:
        response = await provider.complete(
            instructions=INSTRUCTIONS, input_items=items,
            tools=TOOLS if tools is None else tools, reasoning_effort=effort,
            text_schema=SCHEMA,
        )
    except ShadowProviderError as exc:
        print(f"    FAILED retryable={exc.retryable}: {str(exc)[:280]}")
        return None
    print(f"    OK out={[str(i.get('type')) for i in response.output]} "
          f"input_tokens={response.usage.get('input_tokens')} "
          f"reasoning_tokens={(response.usage.get('output_tokens_details') or {}).get('reasoning_tokens')}")
    return [dict(item) for item in response.output]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument(
        "--pad", type=int, default=0,
        help="extra synthetic tool-output payload characters per step (token scale)",
    )
    args = parser.parse_args()

    load_env_file(ROOT / ".env.local")
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("DEEPSEEK_API_KEY missing")
        return 2

    provider = DeepSeekResponsesShadowProvider(
        base_url=args.base_url, api_key=api_key, model=args.model, timeout_ms=90000,
    )
    try:
        trace: list[dict[str, Any]] = [
            {"role": "system", "content": "【权威会话快照】\n用户：帮我在 docs 里核对 TTS 播放、感知延迟和 session.stop 的定义，给出结论。"},
            {"role": "system", "content": "请判断用户当前意图并执行必要的只读工作。"},
        ]
        for step in range(args.steps):
            output = await call(provider, items=trace, label=f"step {step + 1}/{args.steps}")
            if output is None:
                print("\nRESULT: baseline could not be built")
                return 1
            trace.extend(output)
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                print("    (terminal text reached; stopping baseline build)")
                break
            for call_item in calls:
                payload = {"status": "ok", "matches": ["docs/operations-and-quality.md"]}
                if args.pad:
                    payload["excerpt"] = "延迟说明段落。" * max(1, args.pad // 12)
                trace.append({
                    "type": "function_call_output",
                    "call_id": str(call_item.get("call_id") or "invalid"),
                    "output": json.dumps(payload, ensure_ascii=False),
                })
            trace.append({"role": "system", "content": "【权威会话上下文更新】\nframes: 用户补充要求。"})
            trace.append({"role": "system", "content": "请结合最新上下文重新判断意图。"})

        print("\n" + "=" * 70)
        print("CONTINUATION: exact replay of the committed prefix + new activation frame")
        print("=" * 70)
        continuation = list(trace) + [
            {"role": "system", "content": "【权威会话增量】\n用户：只说延迟部分。"},
            {"role": "system", "content": "请结合最新上下文重新判断意图。"},
        ]
        accepted = await call(provider, items=continuation, label="activation 2 (replay)")

        print("\n" + "=" * 70)
        print("CONTROL: same continuation with reasoning items removed (known 400)")
        print("=" * 70)
        stripped = [item for item in continuation if item.get("type") != "reasoning"]
        await call(provider, items=stripped, label="control (reasoning removed)")

        print("\nRESULT: replay " + ("ACCEPTED" if accepted is not None else "REJECTED"))
        return 0 if accepted is not None else 1
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
