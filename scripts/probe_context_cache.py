"""Probe DeepSeek Responses prefix-cache behavior without touching a session.

Run against the official endpoint only.  It sends three sequential requests
with an append-only input history and a changing request-tail clock, then
prints the provider usage values needed for a cache acceptance record.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


STATIC = "你是缓存探针。只用一句中文简短确认收到上下文。"


def tail(round_number: int) -> str:
    # Deliberately dynamic: it must remain in input, after all replayed history.
    now = datetime.now().astimezone().strftime("%H:%M:%S")
    return json.dumps({"runtime": {"clock": {"local_time": now, "precision": "second"},
                                   "probe_round": round_number}}, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--dynamic-instructions", action="store_true",
                        help="counterexample: put the changing clock into instructions")
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
            instructions = STATIC + ("\n动态时间：" + tail(index) if args.dynamic_instructions else "")
            input_items = [*history]
            if not args.dynamic_instructions:
                input_items.append({"role": "system", "content": [{"type": "input_text", "text": tail(index)}]})
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
                              "cache_hit_ratio": ratio}, ensure_ascii=False))
            history.append({"role": "assistant", "content": [{"type": "input_text", "text": "已确认。"}]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
