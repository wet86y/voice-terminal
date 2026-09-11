"""Compare official DeepSeek and OpenCode Go Responses schema acceptance.

Stores only schema structure digests, HTTP metadata, and bounded error text;
never model content, reasoning, prompts, or credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.config import load_config
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane

WEB = {"name": "web_search", "parameters": {"type": "object", "properties": {
    "query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False}}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


async def request(client: httpx.AsyncClient, *, provider: DeepSeekClient, name: str, schema: dict) -> dict:
    frames = [
        ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "仅输出符合给定 JSON schema 的对象。"),
        ContextFrame.make(ContextLane.HISTORY, "user", "请简短确认系统在线。"),
    ]
    payload = DeepSeekClient._responses_payload(frames, model=provider.model, schema=schema,
        schema_name="voice_decision_v06", reasoning_effort="none", max_output_tokens=96)
    # Do not retain instructions/input text; retain only the structural shape.
    payload_meta = {"top_level_keys": sorted(payload), "schema_digest": digest(schema),
                    "action_names": schema["properties"]["actions"]["items"].get("properties", {}).get("name", {}).get("enum", []),
                    "argument_properties": sorted(schema["properties"]["actions"]["items"].get("properties", {}).get("arguments", {}).get("properties", {})),
                    "argument_required": schema["properties"]["actions"]["items"].get("properties", {}).get("arguments", {}).get("required", [])}
    try:
        async with client.stream("POST", provider.base_url.rstrip("/") + "/responses",
                                 headers=provider._request_headers(streaming=True), json=payload) as response:
            body = ""
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")[:1000]
            else:
                # Consume only enough transport events to prove the stream starts; discard all deltas.
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        body = "stream_started"
                        break
            return {"case": name, "status": response.status_code, "accepted": response.status_code < 400,
                    "provider_error": body, "payload_meta": payload_meta}
    except Exception as exc:
        return {"case": name, "status": None, "accepted": False, "provider_error": f"{type(exc).__name__}: {str(exc)[:500]}", "payload_meta": payload_meta}


async def main() -> None:
    config = load_config(ROOT / "config/default.yaml", ROOT / "config/local.yaml")
    open_key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    cases = [("no_actions", decision_schema([], allow_thinking=False)),
             ("thinking", decision_schema([], allow_thinking=True)),
             ("web", decision_schema([WEB], allow_thinking=False)),
             ("web_and_thinking", decision_schema([WEB], allow_thinking=True))]
    targets = [("deepseek", DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model,
                                              api_key=config.api_key(config.llm.api_key_env), provider="deepseek")),
               ("opencode_go", DeepSeekClient(base_url="https://opencode.ai/zen/go/v1", model=config.llm.model,
                                                api_key=open_key, provider="opencode_go", session_id="schema-matrix-v1"))]
    rows = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10)) as client:
        for target, provider in targets:
            for name, schema in cases:
                rows.append({"target": target, **await request(client, provider=provider, name=name, schema=schema)})
    report = {"probe": "responses-schema-provider-matrix", "model": config.llm.model,
              "rows": rows, "generated_content_retained": False, "reasoning_retained": False,
              "prompt_retained": False, "credentials_retained": False}
    path = ROOT / "reports/opencode-vs-deepseek-schema-matrix-20260910.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
