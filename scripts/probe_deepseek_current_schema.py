"""Metadata-only acceptance probe for the current Responses strict schemas."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.config import load_config
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane

WEB = {"name": "web_search", "parameters": {"type": "object", "properties": {
    "query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False}}


async def probe(name: str, schema: dict, client: DeepSeekClient) -> dict:
    events: list[str] = []
    try:
        async for event in client.stream_events([
            ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "仅输出符合 schema 的 JSON。"),
            ContextFrame.make(ContextLane.HISTORY, "user", "请用一句话说明系统在线。"),
        ], schema=schema, reasoning_effort="none"):
            events.append(str(event.get("type")))
        return {"case": name, "accepted": True, "event_types": events,
                "content_or_reasoning_retained": False}
    except Exception as exc:
        return {"case": name, "accepted": False, "error_type": type(exc).__name__,
                "error": " ".join(str(exc).split())[:500], "event_types": events,
                "content_or_reasoning_retained": False}


async def main() -> None:
    config = load_config(ROOT / "config/default.yaml", ROOT / "config/local.yaml")
    client = DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model,
                            api_key=config.api_key(config.llm.api_key_env), provider="deepseek",
                            api_key_env=config.llm.api_key_env, transport="responses", thinking="disabled")
    rows = [
        await probe("no_actions", decision_schema([], allow_thinking=False), client),
        await probe("thinking_only_request_true", decision_schema([], allow_thinking=True), client),
        await probe("thinking_plus_web_search", decision_schema([WEB], allow_thinking=True), client),
    ]
    await client.close()
    output = {"probe": "deepseek-current-strict-schema", "provider": "deepseek",
              "transport": "responses", "model": config.llm.model, "rows": rows,
              "generated_content_retained": False, "reasoning_retained": False}
    path = ROOT / "reports/deepseek-current-schema-acceptance-20260910.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
