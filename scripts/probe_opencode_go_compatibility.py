"""Live, non-persisting compatibility probe for OpenCode Go.

The probe prints only status and structural metadata. It never prints or
stores the API key, model output, reasoning text, or the complete provider
error body.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_terminal.agent.decision_envelope import SpeechDecisionEnvelopeStream
from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.config import load_config
from voice_terminal.providers.deepseek import DeepSeekClient


def _safe_error(exc: Exception, secret: str) -> str:
    return str(exc).replace(secret, "<redacted>").replace("\n", " ")[:240]


async def run(root: Path) -> int:
    config = load_config(root / "config" / "default.yaml", root / "config" / "local.yaml")
    if config.llm.provider != "opencode_go":
        print(f"status=FAIL reason=llm.provider is {config.llm.provider}, expected opencode_go")
        return 2
    api_key = config.api_key(config.llm.api_key_env)
    if not api_key:
        print(f"status=FAIL reason=missing {config.llm.api_key_env}")
        return 2

    client = DeepSeekClient(
        base_url=config.llm.base_url,
        model=config.llm.model,
        api_key=api_key,
        provider=config.llm.provider,
        api_key_env=config.llm.api_key_env,
        session_id=f"probe-{uuid.uuid4().hex}",
        timeout_ms=max(30000, config.llm.timeout_ms),
        transport=config.llm.transport,
        thinking=config.llm.thinking,
        reasoning_effort=config.llm.reasoning_effort,
        max_output_tokens=config.llm.max_output_tokens,
    )
    try:
        http = await client._http_client()
        models_response = await http.get(
            f"{client.base_url}/models", headers=client._request_headers(),
        )
        print(f"models_status={models_response.status_code}")
        if models_response.status_code >= 400:
            print(f"status=FAIL reason={_safe_error(RuntimeError('models request failed'), api_key)}")
            return 1
        try:
            body = models_response.json()
            rows = body.get("data", body) if isinstance(body, dict) else body
            model_ids = {
                str(row.get("id")) for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
        except Exception:
            print("status=FAIL reason=models response was not valid JSON")
            return 1
        print(f"deepseek_flash_available={config.llm.model in model_ids}")

        events = [item async for item in client.stream_events(
            [
                {"role": "system", "content": "Return a valid decision envelope."},
                {"role": "user", "content": "请等待，不要说话。"},
            ],
            schema=decision_schema([]),
            schema_name="voice_decision_v06",
        )]
        parser = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
        content_chars = 0
        for item in events:
            if item.get("type") == "content":
                text = item.get("text")
                if isinstance(text, str):
                    content_chars += len(text)
                    parser.feed(text)
        decision = parser.finish()
        has_usage = any(item.get("type") == "usage" for item in events)
        finish = next((item.get("reason") for item in events if item.get("type") == "finish"), None)
        print(f"responses_event_count={len(events)}")
        print(f"content_chars={content_chars}")
        print(f"decision_state={decision.speak.state}")
        print(f"usage_present={has_usage}")
        print(f"finish={finish}")
        ok = finish == "completed" and has_usage and decision.speak.state == "wait"
        print(f"status={'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    except Exception as exc:
        print(f"status=FAIL reason={_safe_error(exc, api_key)}")
        return 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    return asyncio.run(run(args.root.resolve()))


if __name__ == "__main__":
    sys.exit(main())
