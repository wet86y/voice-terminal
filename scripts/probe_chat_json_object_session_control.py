"""Control probe: legacy Chat Completions JSON-object transport.

This is the matched control for ``probe_responses_json_schema_session.py``.
It replays the exact same real ASR-final subset with thinking disabled and a
terminal-aware whitespace-tolerant SSE collector.  It never saves generated
model wording and never invokes TTS/PCM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probe_responses_json_schema_session import (
    SELECTED_CASES,
    SESSION_ASR_FINALS,
    SYSTEM_INSTRUCTIONS,
)
from voice_terminal.agent.decision_envelope import EnvelopeError, SpeechDecisionEnvelopeStream
from voice_terminal.config import load_config


def ms(start: float, mark: float | None) -> float | None:
    return round((mark - start) * 1000, 1) if mark is not None else None


def messages(case_index: int) -> list[dict[str, str]]:
    history = SESSION_ASR_FINALS[: case_index - 1]
    prompt = SYSTEM_INSTRUCTIONS + (
        "\n最终 content 必须且只能是 JSON 对象："
        '{"speak":{"state":"speak","emotion":"calm","text":"非空中文"},"actions":[]}。'
    )
    result: list[dict[str, str]] = [{"role": "system", "content": prompt}]
    if history:
        result.append({
            "role": "user",
            "content": "此前真实会话的用户 ASR final 转写（仅作上下文，没有保存助手回复）：\n"
            + "\n".join(f"用户第{i + 1}轮：{text}" for i, text in enumerate(history)),
        })
    result.append({"role": "user", "content": SESSION_ASR_FINALS[case_index - 1]})
    return result


async def invoke(*, client: Any, base_url: str, model: str, key: str, case_index: int,
                 max_tokens: int | None) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages(case_index),
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    started = time.perf_counter()
    marks: dict[str, float | None] = {"headers": None, "first_sse": None, "first_output_delta": None,
                                      "first_non_whitespace": None, "terminal": None}
    status: int | None = None
    error: str | None = None
    finish_reason: str | None = None
    chunks: list[str] = []
    events: Counter[str] = Counter()
    output_deltas = blank_deltas = leading_whitespace = 0
    before_non_whitespace = True
    try:
        async with client.stream("POST", f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload) as response:
            status = response.status_code
            marks["headers"] = time.perf_counter()
            if status >= 400:
                error = (await response.aread()).decode("utf-8", errors="replace")[:800]
            else:
                async for line in response.aiter_lines():
                    now = time.perf_counter()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        marks["terminal"] = now
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        events["unparseable_data"] += 1
                        continue
                    events["chunk"] += 1
                    if marks["first_sse"] is None:
                        marks["first_sse"] = now
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    if choice.get("finish_reason") is not None:
                        marks["terminal"] = now
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    content = delta.get("content")
                    if not isinstance(content, str):
                        continue
                    output_deltas += 1
                    if marks["first_output_delta"] is None:
                        marks["first_output_delta"] = now
                    chunks.append(content)
                    if not content.strip():
                        blank_deltas += 1
                    elif before_non_whitespace:
                        marks["first_non_whitespace"] = now
                        before_non_whitespace = False
                    if before_non_whitespace:
                        leading_whitespace += len(content)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"

    content = "".join(chunks)
    shape = None
    try:
        if status != 200 or finish_reason != "stop":
            raise EnvelopeError(f"non-completed Chat terminal: {finish_reason}")
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            shape = {"top_level_keys": sorted(str(key) for key in parsed),
                     "speak_keys": sorted(str(key) for key in parsed.get("speak", {})) if isinstance(parsed.get("speak"), dict) else None,
                     "actions_is_list": isinstance(parsed.get("actions"), list)}
        stream = SpeechDecisionEnvelopeStream(); stream.feed(content); decision = stream.finish()
        validation = {"valid": True, "state": decision.speak.state, "emotion": decision.speak.emotion,
                      "text_chars": len(decision.speak.text), "action_count": len(decision.actions)}
    except (EnvelopeError, ValueError) as exc:
        validation = {"valid": False, "error": str(exc)[:300]}
    return {
        "http_status": status, "finish_reason": finish_reason, "provider_error": error,
        "latency_ms": {key: ms(started, value) for key, value in marks.items()},
        "stream": {"event_counts": dict(events), "output_delta_count": output_deltas,
                   "blank_output_delta_count": blank_deltas, "leading_whitespace_chars": leading_whitespace,
                   "content_chars": len(content)},
        "validation": validation, "structural_shape": shape,
    }


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = [row["latency_ms"]["first_non_whitespace"] for row in rows if row["latency_ms"]["first_non_whitespace"] is not None]
    terminal = [row["latency_ms"]["terminal"] for row in rows if row["latency_ms"]["terminal"] is not None]
    return {
        "samples": len(rows), "http_200": sum(row["http_status"] == 200 for row in rows),
        "completed_stop": sum(row["finish_reason"] == "stop" for row in rows),
        "local_envelope_valid": sum(row["validation"]["valid"] for row in rows),
        "state_matches_expected": sum(row["validation"].get("state") == row["expected_state"] for row in rows),
        "blank_only_streams": sum(row["stream"]["content_chars"] > 0 and row["stream"]["content_chars"] == row["stream"]["leading_whitespace_chars"] for row in rows),
        "leading_whitespace_observed": sum(row["stream"]["leading_whitespace_chars"] > 0 for row in rows),
        "first_non_whitespace_ms": {"median": round(statistics.median(first), 1) if first else None,
                                     "max": round(max(first), 1) if first else None},
        "terminal_ms": {"median": round(statistics.median(terminal), 1) if terminal else None,
                        "max": round(max(terminal), 1) if terminal else None},
        "errors": dict(Counter(str(row["validation"].get("error")) for row in rows if not row["validation"]["valid"])),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import httpx
    config = load_config((ROOT / args.config).resolve(), (ROOT / args.local_config).resolve())
    key = config.api_key(config.llm.api_key_env)
    if not key:
        raise RuntimeError(f"未配置 {config.llm.api_key_env}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout))) as client:
        rows = []
        for position, name, expected in SELECTED_CASES:
            rows.append({"case": name, "session_turn": position, "expected_state": expected,
                         "input_chars": len(SESSION_ASR_FINALS[position - 1]), "history_turn_count": position - 1,
                         **await invoke(client=client, base_url=config.llm.base_url, model=config.llm.model,
                                        key=key, case_index=position,
                                        max_tokens=None if args.omit_max_tokens else args.max_tokens)})
    return {"probe": "chat-json-object-real-session-control", "model": config.llm.model,
            "transport": "DeepSeek Chat Completions + response_format=json_object + thinking disabled + terminal-aware whitespace-tolerant SSE collector",
            "case_count": len(SELECTED_CASES), "max_tokens": None if args.omit_max_tokens else args.max_tokens,
            "generated_content_retained": False, "tts_or_pcm_started": False,
            "rows": rows, "summary": summary(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--local-config", default="config/local.yaml")
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--omit-max-tokens", action="store_true", help="Match the current live provider payload exactly.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
