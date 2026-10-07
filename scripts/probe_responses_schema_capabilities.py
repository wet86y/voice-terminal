"""Probe DeepSeek's accepted JSON-Schema subset without retaining model text.

The report contains only HTTP status, terminal status, timing and a coarse
error classification. It never starts TTS/tools and never stores response or
reasoning content.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.decision_envelope import SpeechDecisionEnvelopeStream
from voice_terminal.config import load_config


ACTION_DEFINITIONS = [
    {
        "name": "web_search",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 256}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_read_text",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


def cases():
    no_actions = decision_schema([])
    tools = decision_schema(ACTION_DEFINITIONS)
    union = copy.deepcopy(tools)
    union["properties"]["speak"] = {
        "oneOf": [
            {"type": "object", "required": ["state", "text"], "properties": {
                "state": {"type": "string", "enum": ["speak"]}, "text": {"type": "string", "minLength": 1}}},
            {"type": "object", "required": ["state"], "properties": {
                "state": {"type": "string", "enum": ["wait", "cancel"]}}},
        ]
    }
    return {
        "production_no_actions": no_actions,
        "production_tools": tools,
        "diagnostic_discriminated_union": union,
    }


async def run(args):
    import httpx

    config = load_config(ROOT / args.config, ROOT / args.local_config)
    key = config.api_key(config.llm.api_key_env)
    if not key:
        raise RuntimeError(f"未配置 {config.llm.api_key_env}")
    rows = []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for name, schema in cases().items():
            payload = {
                "model": config.llm.model,
                "instructions": (
                    "Return one valid voice decision. There are no executable tools in this probe. "
                    "Every declared property is required by the provider schema. For wait/cancel, "
                    "set speak.text to the empty string. Set irrelevant action argument fields to "
                    "empty or neutral values."
                ),
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "请等待。"}]}],
                "reasoning": {"effort": "none"},
                "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
                "max_output_tokens": 128,
            }
            started = time.perf_counter()
            status = None
            terminal = None
            error_code = None
            local_valid = False
            try:
                response = await client.post(config.llm.base_url.rstrip("/") + "/responses", headers=headers, json=payload)
                status = response.status_code
                if response.status_code == 200:
                    value = response.json()
                    terminal = value.get("status") if isinstance(value, dict) else None
                    if args.debug_errors and isinstance(value, dict):
                        shape = []
                        for item in value.get("output", []):
                            if not isinstance(item, dict):
                                continue
                            shape.append({"type": item.get("type"), "keys": sorted(item), "content": [
                                {"type": part.get("type"), "keys": sorted(part),
                                 "text_chars": len(part.get("text", "")) if isinstance(part.get("text"), str) else None}
                                for part in item.get("content", []) if isinstance(part, dict)
                            ]})
                        print(f"[{name}] response shape: {json.dumps(shape, ensure_ascii=False)}", file=sys.stderr)
                    parts = []
                    for item in value.get("output", []) if isinstance(value, dict) else []:
                        for content in item.get("content", []) if isinstance(item, dict) else []:
                            if isinstance(content, dict) and content.get("type") == "output_text":
                                text = content.get("text")
                                if isinstance(text, str):
                                    parts.append(text)
                    try:
                        envelope = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
                        envelope.feed("".join(parts))
                        envelope.finish()
                        local_valid = True
                    except ValueError as exc:
                        error_code = "local_envelope_invalid"
                        if args.debug_errors:
                            print(f"[{name}] local validation: {exc}", file=sys.stderr)
                else:
                    # Provider bodies can echo schema/prompt details; retain
                    # only a stable class, never the body.
                    error_code = "schema_rejected" if response.status_code == 400 else "http_error"
                    if args.debug_errors:
                        print(f"[{name}] HTTP {response.status_code}: {response.text[:2000]}", file=sys.stderr)
            except Exception as exc:
                error_code = type(exc).__name__
            rows.append({
                "case": name,
                "http_status": status,
                "terminal_status": terminal,
                "accepted": status == 200,
                "local_envelope_valid": local_valid,
                "error_code": error_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            })
    return {"probe": "responses-schema-capabilities", "model": config.llm.model, "rows": rows,
            "generated_content_retained": False, "tts_or_tools_started": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--local-config", default="config/local.yaml")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--debug-errors", action="store_true",
                        help="Print provider error bodies for this synthetic-schema probe only.")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
