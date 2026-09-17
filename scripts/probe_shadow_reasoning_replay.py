"""Reproduce the Shadow "reasoning must be passed back" 400 with real provider calls.

The Shadow runtime commits every ``response.completed.output`` item verbatim and
replays the whole committed trace as the next request's ``input``.  This probe
performs the same two-step loop with real HTTP calls and dumps the exact wire
shape of every committed item, so a provider-side rejection can be attributed to
the item that DeepSeek dislikes instead of being guessed from the UI text.

Usage (repo root):

    python -m scripts.probe_shadow_reasoning_replay
    python -m scripts.probe_shadow_reasoning_replay --effort high --variant full
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

INSTRUCTIONS = (
    "你是只读研究助手。需要外部事实时必须调用工具；已有足够信息时直接给出结论。"
)

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

TERMINAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["state", "text"],
    "properties": {"state": {"type": "string"}, "text": {"type": "string"}},
}


def _strip_volatile(value: Any) -> Any:
    """Remove ids/status noise so the two shapes can be compared structurally."""

    if isinstance(value, dict):
        return {
            key: _strip_volatile(item) for key, item in value.items()
            if key not in {"id", "status", "call_id", "created_at"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _describe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "type": item.get("type"),
            "keys": sorted(item.keys()),
            "status": item.get("status"),
        }
        content = item.get("content")
        if isinstance(content, list):
            row["content_types"] = [
                block.get("type") for block in content if isinstance(block, dict)
            ]
            row["content_text_chars"] = [
                len(block.get("text") or "") for block in content
                if isinstance(block, dict)
            ]
        elif isinstance(content, str):
            row["content_is_str"] = True
        if "summary" in item:
            row["has_summary"] = True
        if "encrypted_content" in item:
            row["has_encrypted_content"] = item.get("encrypted_content") is not None
        rows.append(row)
    return rows


async def _call(
    provider: DeepSeekResponsesShadowProvider, *,
    input_items: list[dict[str, Any]], tools: list[dict[str, Any]],
    effort: str, schema: dict[str, Any] | None, label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    print(f"\n=== {label}: POST /responses effort={effort} tools={[t['name'] for t in tools]} ===")
    print("input item types: " + json.dumps(
        [item.get("type") or item.get("role") for item in input_items], ensure_ascii=False,
    ))
    try:
        response = await provider.complete(
            instructions=INSTRUCTIONS, input_items=input_items, tools=tools,
            reasoning_effort=effort, text_schema=schema,
        )
    except ShadowProviderError as exc:
        print(f"FAILED: {exc}")
        print(f"retryable={exc.retryable}")
        raise
    print(f"ok: output_items={len(response.output)} usage={json.dumps(response.usage, ensure_ascii=False)}")
    print("output shape: " + json.dumps(_describe(list(response.output)), ensure_ascii=False, indent=2))
    return list(response.output), response.usage


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--effort", default="high")
    parser.add_argument(
        "--variant", choices=(
            "full", "no_reasoning", "keys_only", "text_only",
            "no_content", "no_encrypted", "empty_content", "no_summary",
        ),
        default="full",
        help=(
            "full: replay committed items verbatim (current runtime behaviour); "
            "no_reasoning: drop reasoning items; keys_only: keep reasoning type only; "
            "text_only: keep only reasoning_text content blocks; "
            "no_content: drop the reasoning_text content array; "
            "empty_content: replace content with []; "
            "no_encrypted: drop encrypted_content; no_summary: drop summary."
        ),
    )
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
        first_input = [
            {"role": "system", "content": "【权威会话快照】\n用户：帮我查一下 docs 里有没有关于 TTS 的文档。"},
            {"role": "system", "content": "请判断用户当前意图并执行必要的只读工作。"},
        ]
        output_one, _ = await _call(
            provider, input_items=first_input, tools=TOOLS, effort=args.effort,
            schema=TERMINAL_SCHEMA, label="step 1 (expect a function_call)",
        )
        committed = [dict(item) for item in output_one]
        calls = [item for item in committed if item.get("type") == "function_call"]
        for call in calls:
            committed.append({
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or "invalid"),
                "output": json.dumps({"status": "ok", "matches": ["docs/operations-and-quality.md"]}, ensure_ascii=False),
            })
        committed.append({"role": "system", "content": "上下文未变化，请给出结论。"})

        replay = _apply_variant(committed, args.variant)
        print("\n--- replay variant: " + args.variant + " ---")
        print("replayed shape: " + json.dumps(_describe(replay), ensure_ascii=False, indent=2))
        if args.variant == "text_only":
            print("note: text_only keeps blocks the runtime never produces; used as control")
        print("replayed reasoning items (raw): " + json.dumps(
            [item for item in replay if item.get("type") == "reasoning"],
            ensure_ascii=False, indent=2,
        )[:4000])
        await _call(
            provider, input_items=replay, tools=TOOLS, effort=args.effort,
            schema=TERMINAL_SCHEMA, label="step 2 (replay committed trace)",
        )
        print("\nRESULT: replay accepted by provider")
        return 0
    finally:
        await provider.close()


def _apply_variant(items: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    if variant == "full":
        return [dict(item) for item in items]
    if variant == "no_reasoning":
        return [dict(item) for item in items if item.get("type") != "reasoning"]
    if variant == "keys_only":
        result: list[dict[str, Any]] = []
        for item in items:
            copy = dict(item)
            if copy.get("type") == "reasoning":
                copy.pop("content", None)
                copy.pop("summary", None)
            result.append(copy)
        return result
    if variant == "no_content":
        result = []
        for item in items:
            copy = dict(item)
            if copy.get("type") == "reasoning":
                copy.pop("content", None)
            result.append(copy)
        return result
    if variant == "empty_content":
        result = []
        for item in items:
            copy = dict(item)
            if copy.get("type") == "reasoning":
                copy["content"] = []
            result.append(copy)
        return result
    if variant == "no_encrypted":
        result = []
        for item in items:
            copy = dict(item)
            if copy.get("type") == "reasoning":
                copy.pop("encrypted_content", None)
            result.append(copy)
        return result
    if variant == "no_summary":
        result = []
        for item in items:
            copy = dict(item)
            if copy.get("type") == "reasoning":
                copy.pop("summary", None)
            result.append(copy)
        return result
    # text_only
    result = []
    for item in items:
        copy = dict(item)
        if copy.get("type") == "reasoning":
            content = copy.get("content")
            if isinstance(content, list):
                copy["content"] = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "reasoning_text"
                ]
        result.append(copy)
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
