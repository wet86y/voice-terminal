"""Probe DeepSeek Responses prefix-cache behavior without touching a session.

Run against the official endpoint only.  It sends three sequential requests
with a deliberately long stable prefix, append-only input history and a
changing request-tail clock.  ``--dynamic-speak`` also advances the four-part
Speak projection in that tail.  The delay between rounds gives DeepSeek's
best-effort cache time to persist prefix units before usage is inspected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime


STATIC = "你是缓存探针。只用一句中文简短确认收到上下文。\n" + "\n".join(
    f"稳定协议条款 {index:03d}：本条只用于构造可观测的固定前缀，不包含动态时间或运行状态。"
    for index in range(128)
)


def tail(round_number: int, *, dynamic_speak: bool = False) -> str:
    # Deliberately dynamic: it must remain in input, after all replayed history.
    now = datetime.now().astimezone().strftime("%H:%M:%S")
    runtime = {
        "clock": {"local_time": now, "precision": "second"},
        "probe_round": round_number,
    }
    if dynamic_speak:
        parts = (
            ("", "第一句。", "", "第二句。第三句。"),
            ("第一句。", "第二句。", "", "第三句。"),
            ("第一句。", "第二句。", "第三句。", ""),
        )[min(2, max(0, round_number - 1))]
        runtime["speak"] = {
            "state": "speaking",
            "owner_scope": "current_turn",
            "continuation_mode": "rewrite_tail" if parts[3] else "append_only",
            "text_parts": {
                "committed_prefix": parts[0],
                "current_clause": parts[1],
                "prefetched_clause": parts[2],
                "revisable_tail": parts[3],
            },
        }
    return json.dumps({"runtime": runtime}, ensure_ascii=False, separators=(",", ":"))


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--dynamic-instructions", action="store_true",
                        help="counterexample: put the changing clock into instructions")
    parser.add_argument("--dynamic-speak", action="store_true",
                        help="vary the four-part active Speak only in the request tail")
    parser.add_argument("--settle-seconds", type=float, default=2.0,
                        help="wait for best-effort cache persistence between rounds")
    args = parser.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    try:
        import httpx
    except ImportError:
        print("httpx is required; run scripts\\setup.ps1", file=sys.stderr)
        return 2

    history: list[dict] = []
    with httpx.Client(timeout=60) as client:
        for index, text in enumerate(("第一轮：确认缓存基准。", "第二轮：历史追加。", "第三轮：继续追加。"), 1):
            history.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
            dynamic_tail = tail(index, dynamic_speak=args.dynamic_speak)
            instructions = STATIC + ("\n动态时间：" + dynamic_tail if args.dynamic_instructions else "")
            input_items = [*history]
            if not args.dynamic_instructions:
                input_items.append({"role": "system", "content": [{"type": "input_text", "text": dynamic_tail}]})
            response = client.post(args.base_url.rstrip("/") + "/responses", headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            }, json={"model": args.model, "instructions": instructions, "input": input_items,
                     "max_output_tokens": 32})
            response.raise_for_status()
            usage = response.json().get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            input_tokens = usage.get("input_tokens")
            cached = details.get("cached_tokens")
            ratio = cached / input_tokens if isinstance(cached, int) and isinstance(input_tokens, int) and input_tokens else None
            print(json.dumps({"round": index, "input_tokens": input_tokens, "cached_tokens": cached,
                              "cache_miss_tokens": input_tokens - cached if isinstance(input_tokens, int) and isinstance(cached, int) else None,
                              "cache_hit_ratio": ratio,
                              "static_digest": digest(instructions),
                              "history_digest": digest(history)}, ensure_ascii=False))
            history.append({"role": "assistant", "content": [{"type": "input_text", "text": "已确认。"}]})
            if index < 3 and args.settle_seconds > 0:
                time.sleep(args.settle_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
