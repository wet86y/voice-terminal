"""Drive the real ShadowRuntime against the real provider and capture any 400.

This is the end-to-end reproduction lane.  It builds a genuine
``ShadowRuntime`` around the real ``DeepSeekResponsesShadowProvider`` and a real
``SessionLedger``, then runs two activations separated by a new authoritative
user revision -- exactly the sequence recorded in ``logs/voice-server.log``
where activation serial 1 finished with 6 committed steps and serial 2 failed
its very first provider request with::

    Responses HTTP 400: The `reasoning_text` in the thinking mode must be
    passed back to the API.

Every provider request is recorded (input item types, whether reasoning items
carry their ``reasoning_text`` content, usage) so a rejection can be attributed
to a specific input shape rather than guessed.
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

from voice_terminal.agent.tool_registry import FileTools  # noqa: E402
from voice_terminal.env import load_env_file  # noqa: E402
from voice_terminal.session.ledger import SessionLedger  # noqa: E402
from voice_terminal.shadow_agent.manager import ShadowRuntime  # noqa: E402
from voice_terminal.shadow_agent.provider import (  # noqa: E402
    DeepSeekResponsesShadowProvider, ShadowProviderError,
)
from voice_terminal.shadow_agent.tools import ShadowToolExecutor  # noqa: E402


def summarize_input(items: Any) -> tuple[str, bool]:
    """Return a compact item-type trace and whether reasoning text survived."""

    kinds: list[str] = []
    reasoning_ok = True
    for item in items:
        if not isinstance(item, dict):
            kinds.append("?")
            continue
        kind = item.get("type") or f"role={item.get('role')}"
        kinds.append(str(kind))
        if item.get("type") == "reasoning":
            content = item.get("content")
            has_text = (
                isinstance(content, list)
                and any(
                    isinstance(block, dict) and block.get("type") == "reasoning_text"
                    and (block.get("text") or "").strip()
                    for block in content
                )
            )
            if not has_text:
                reasoning_ok = False
    return " | ".join(kinds), reasoning_ok


class RecordingProvider:
    """Real provider wrapper that records every request and its outcome."""

    def __init__(self, inner: DeepSeekResponsesShadowProvider, log: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._log = log

    async def complete(self, **kwargs: Any):
        items = list(kwargs.get("input_items") or ())
        kinds, reasoning_ok = summarize_input(items)
        record: dict[str, Any] = {
            "effort": kwargs.get("reasoning_effort"),
            "tools": [tool.get("name") for tool in kwargs.get("tools") or ()],
            "schema": bool(kwargs.get("text_schema")),
            "max_output_tokens": kwargs.get("max_output_tokens"),
            "input_count": len(items),
            "input_kinds": kinds,
            "reasoning_items": sum(1 for i in items if isinstance(i, dict) and i.get("type") == "reasoning"),
            "reasoning_text_present": reasoning_ok,
            "input_items": items,
        }
        self._log.append(record)
        print(f"\n>>> request #{len(self._log)} effort={record['effort']} "
              f"tools={record['tools']} schema={record['schema']} "
              f"input={record['input_count']} reasoning={record['reasoning_items']} "
              f"reasoning_text_ok={record['reasoning_text_present']}")
        print("    " + kinds[:500])
        try:
            response = await self._inner.complete(**kwargs)
        except ShadowProviderError as exc:
            record["error"] = str(exc)
            record["retryable"] = exc.retryable
            print(f"    FAILED retryable={exc.retryable}: {str(exc)[:280]}")
            raise
        record["usage"] = response.usage
        record["output_items"] = [dict(item) for item in response.output]
        print(f"    OK out={[str(i.get('type')) for i in response.output]} "
              f"reasoning_tokens={(response.usage.get('output_tokens_details') or {}).get('reasoning_tokens')} "
              f"cached={response.usage.get('cached_tokens')}/{response.usage.get('input_tokens')} "
              f"replay_fallback={response.replay_output is not None}")
        return response

    async def close(self) -> None:
        await self._inner.close()


async def wait_idle(runtime: ShadowRuntime, *, timeout: float = 240.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        observer = runtime.observer_task
        if not runtime.active and (observer is None or observer.done()):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("shadow runtime did not become idle")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--dump", default="", help="write the failing input to this JSON path")
    parser.add_argument("--no-observer", action="store_true")
    parser.add_argument("--web-search", action="store_true")
    args = parser.parse_args()

    load_env_file(ROOT / ".env.local")
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("DEEPSEEK_API_KEY missing")
        return 2

    records: list[dict[str, Any]] = []
    inner = DeepSeekResponsesShadowProvider(
        base_url=args.base_url, api_key=api_key, model=args.model, timeout_ms=60000,
    )
    provider = RecordingProvider(inner, records)

    instructions = (ROOT / "config" / "shadow_agent" / "instructions.md")
    loader_text = instructions.read_text(encoding="utf-8") if instructions.is_file() else "你是只读研究助手。"

    ledger = SessionLedger()
    events: list[Any] = []
    executor = ShadowToolExecutor(FileTools((ROOT / "docs",)))
    runtime = ShadowRuntime(
        provider, executor, ledger,
        instructions_loader=lambda: loader_text,
        on_event=events.append,
        observer_enabled=not args.no_observer,
        reasoning_effort=args.effort,
        allowed_reasoning=("off", "low", "medium", "high", "max"),
    )
    try:
        print("=" * 78)
        print("ACTIVATION 1")
        print("=" * 78)
        ledger.commit_input(
            "user-1",
            "帮我在 docs 里查一下 TTS 播放和感知延迟的定义，并说明 session.stop 会不会取消已经排队的播报。",
        )
        runtime.wake()
        await wait_idle(runtime)
        await asyncio.sleep(0.5)
        print(f"\nactivation 1 done: committed_steps={runtime.committed_model_steps} "
              f"trace_items={len(runtime.working_trace)}")
        print("committed trace kinds: " + " | ".join(
            str(item.get("type") or f"role={item.get('role')}") for item in runtime.working_trace
        ))
        print("events: " + json.dumps(
            [{"kind": event.kind, "chars": len(event.text or "")} for event in events],
            ensure_ascii=False,
        ))

        print("\n" + "=" * 78)
        print("ACTIVATION 2 (new authoritative revision, same committed trace)")
        print("=" * 78)
        events.clear()
        ledger.commit_input("user-2", "补充：只说延迟那一部分，别的先不用管。")
        runtime.wake()
        try:
            await wait_idle(runtime)
        except TimeoutError as exc:
            print(f"wait_idle: {exc}")
        await asyncio.sleep(0.5)
        print(f"\nactivation 2 done: committed_steps={runtime.committed_model_steps} "
              f"trace_items={len(runtime.working_trace)}")
        print("events: " + json.dumps(
            [{"kind": event.kind, "chars": len(event.text or ""), "text": (event.text or "")[:160]}
             for event in events],
            ensure_ascii=False, indent=2,
        ))

        failures = [record for record in records if record.get("error")]
        print("\n" + "=" * 78)
        print(f"SUMMARY: {len(records)} requests, {len(failures)} failed")
        for record in failures:
            print(f"  request #{records.index(record) + 1}: effort={record['effort']} "
                  f"tools={record['tools']} schema={record['schema']} "
                  f"input={record['input_count']} reasoning_items={record['reasoning_items']} "
                  f"reasoning_text_ok={record['reasoning_text_present']}")
            print(f"    {str(record['error'])[:300]}")
        if failures and args.dump:
            failing = failures[0]
            Path(args.dump).write_text(
                json.dumps(failing["input_items"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"failing input written to {args.dump}")
        return 1 if failures else 0
    finally:
        await runtime.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
