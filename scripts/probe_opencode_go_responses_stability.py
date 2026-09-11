"""High-frequency, metadata-only three-round Responses stability probe.

This probe uses the configured OpenCode Go profile through a process-level
provider override. It never starts the application, TTS, tools, or audio.
Reports intentionally retain no generated text or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.config import load_config
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane

STATIC = (
    "你是持续监听的中文语音 Agent。每次只返回严格 JSON 决策对象，不要 Markdown 或解释。"
    "对象必须包含 speak 与 actions；speak.state 只能是 speak/cancel/wait，"
    "speak.emotion 只能是 calm/happy/sad/angry/fearful/disgusted/surprised，"
    "speak.text 必须是字符串；actions 必须为空数组。"
)
PROMPTS = (
    "请简短确认你听见了我。",
    "上一轮播报已经播放完成。请保持协议稳定，说明当前是否需要继续回应。",
    "这是第三轮协议探测，请给出最小必要的语音决策。",
)


def _frame(lane: ContextLane, role: str, content: str) -> dict[str, str]:
    return ContextFrame.make(lane, role, content)


def _validate(raw: str) -> tuple[bool, str | None, str]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return False, None, "invalid_json"
    if not isinstance(obj, dict) or set(obj) != {"speak", "actions"}:
        return False, None, "envelope_keys"
    speak = obj.get("speak")
    if not isinstance(speak, dict) or set(speak) - {"state", "emotion", "text"}:
        return False, None, "speak_shape"
    state = speak.get("state")
    if state not in {"speak", "cancel", "wait"}:
        return False, None, "state"
    if speak.get("emotion", "calm") not in {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}:
        return False, state, "emotion"
    if obj.get("actions") != []:
        return False, state, "actions"
    if state == "speak" and (not isinstance(speak.get("text"), str) or not speak["text"].strip()):
        return False, state, "speak_text"
    if state != "speak" and "text" in speak and speak.get("text") not in {"", None}:
        return False, state, "control_text"
    return True, state, ""


def _error_kind(exc: Exception) -> str:
    text = str(exc).lower()
    if "connect" in text or "tls" in text:
        return "connect_error"
    if "http" in text:
        return "http_error"
    if "terminal" in text or "incomplete" in text or "disconnected" in text:
        return "stream_terminal"
    if "timeout" in text:
        return "timeout"
    return type(exc).__name__


async def _one_session(*, config: Any, index: int, rounds: int, timeout: float,
                       max_tokens: int, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
    session_id = f"stability-{uuid.uuid4().hex}"
    client = DeepSeekClient(
        base_url=config.llm.base_url, model=config.llm.model,
        api_key=config.api_key(config.llm.api_key_env), provider="opencode_go",
        api_key_env=config.llm.api_key_env, session_id=session_id,
        timeout_ms=max(1000, int(timeout * 1000)), transport="responses",
        thinking=config.llm.thinking, reasoning_effort=config.llm.reasoning_effort,
        max_output_tokens=max_tokens,
    )
    schema = decision_schema([])
    rows: list[dict[str, Any]] = []
    history: list[dict[str, str]] = []
    try:
        for round_no in range(1, rounds + 1):
            current = PROMPTS[round_no - 1]
            messages = [_frame(ContextLane.STATIC_INSTRUCTIONS, "system", STATIC)]
            messages.extend(history)
            messages.append(_frame(ContextLane.REQUEST_TAIL, "user", current))
            messages.append(_frame(
                ContextLane.REQUEST_TAIL, "system",
                json.dumps({"runtime": {"clock": {"precision": "second"}, "speak": {"state": "empty"}}}, ensure_ascii=False),
            ))
            started = time.perf_counter()
            raw_parts: list[str] = []
            events: list[dict[str, Any]] = []
            error: str | None = None
            async with sem:
                try:
                    async for event in client.stream_events(messages, schema=schema, schema_name="voice_decision_v06"):
                        events.append(event)
                        if event.get("type") == "content" and isinstance(event.get("text"), str):
                            raw_parts.append(event["text"])
                except Exception as exc:
                    error = _error_kind(exc)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            raw = "".join(raw_parts)
            valid, state, validation_error = _validate(raw) if error is None else (False, None, error)
            usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "usage" and isinstance(e.get("usage"), dict)), {})
            finish = next((e.get("reason") for e in reversed(events) if e.get("type") == "finish"), None)
            terminal_ok = finish == "completed"
            rows.append({
                "session": index, "round": round_no, "latency_ms": elapsed_ms,
                "terminal": terminal_ok, "finish": finish, "valid_json_envelope": valid,
                "state": state, "validation_error": validation_error or None,
                "error_kind": error, "content_chars": len(raw),
                "usage": {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "cached_tokens", "cache_miss_tokens") if key in usage},
                "event_count": len(events),
            })
            if valid:
                try:
                    output = json.loads(raw)
                    heard = output.get("speak", {}).get("text", "") if state == "speak" else ""
                except Exception:
                    heard = ""
            else:
                heard = ""
            # The next round intentionally replays only settled history.
            history.extend([_frame(ContextLane.HISTORY, "user", current), _frame(ContextLane.HISTORY, "assistant", heard)])
    finally:
        await client.close()
    return rows


async def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(ROOT / "config" / "default.yaml", ROOT / "config" / "local.yaml")
    if config.llm.provider != "opencode_go":
        raise RuntimeError(f"provider override failed: {config.llm.provider}")
    key = config.api_key(config.llm.api_key_env)
    if not key:
        raise RuntimeError(f"missing {config.llm.api_key_env}")
    sem = asyncio.Semaphore(args.concurrency)
    batches = await asyncio.gather(*[
        _one_session(config=config, index=i, rounds=3, timeout=args.timeout,
                     max_tokens=args.max_tokens, sem=sem)
        for i in range(1, args.samples + 1)
    ])
    rows = [row for batch in batches for row in batch]
    by_round: dict[str, Any] = {}
    for round_no in (1, 2, 3):
        group = [row for row in rows if row["round"] == round_no]
        latencies = [row["latency_ms"] for row in group]
        usage_rows = [row["usage"] for row in group if row["usage"]]
        by_round[str(round_no)] = {
            "samples": len(group), "terminal_completed": sum(row["terminal"] for row in group),
            "valid_json_envelope": sum(row["valid_json_envelope"] for row in group),
            "errors": dict(Counter(row["error_kind"] or row["validation_error"] for row in group if row["error_kind"] or row["validation_error"])),
            "states": dict(Counter(row["state"] for row in group if row["state"])),
            "latency_ms": {"min": min(latencies), "p50": statistics.median(latencies), "max": max(latencies)},
            "usage_present": len(usage_rows),
            "cached_tokens_present": sum("cached_tokens" in usage for usage in usage_rows),
            "cache_miss_tokens_present": sum("cache_miss_tokens" in usage for usage in usage_rows),
        }
    return {
        "probe": "opencode-go-responses-three-round-stability", "provider": config.llm.provider,
        "base_url": config.llm.base_url, "model": config.llm.model, "transport": "responses",
        "samples": args.samples, "rounds_per_sample": 3, "total_requests": len(rows),
        "concurrency": args.concurrency, "tts_or_tools_started": False,
        "round_summary": by_round, "rows": rows,
        "protocol_blocking_issue": any(not row["terminal"] or not row["valid_json_envelope"] for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "round_summary": report["round_summary"], "protocol_blocking_issue": report["protocol_blocking_issue"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
