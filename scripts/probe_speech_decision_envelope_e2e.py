"""Test the proposed required ``speak`` response-envelope transport.

Unlike the current native ``speak`` function, this experiment sends no tools
to DeepSeek.  Each assistant response must be one JSON object whose required
semantic decision is ``speak.state``.  Synthetic post-ASR text drives two
consecutive utterances; an unchanged finalization is intentionally handled
locally and does not create another model request.

This is a diagnostics-only experiment.  It does not alter the live runtime,
does not open TTS, and stores no generated text or reasoning in its report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_terminal.config import load_config
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.ledger import SessionLedger


FIRST_TEXT = "你好啊，你听到我在讲什么吗？"
SECOND_TEXT = "那请再用一句话告诉我，你刚才听到我说了什么。"
EMOTIONS = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}

ENVELOPE_PROMPT = """你是持续监听的中文语音 Agent。你的每一次最终响应都是本地语音系统的状态声明，
不是普通聊天正文，也不是一次可选工具调用。

最终 content 必须且只能是一个 JSON 对象，严格使用此形状：
{"speak":{"state":"speak 或 wait","emotion":"可选情绪","text":"仅 speak 时使用"},"actions":[]}

规则：
- 每轮必须有且只有一个 speak 对象；禁止 JSON 外的解释、Markdown 或普通正文。
- state 只能精确使用英文 speak 或 wait；不得使用其它状态名。
- emotion 可以省略；若提供，只能精确使用英文 calm、happy、sad、angry、fearful、disgusted、surprised 之一，不能使用 friendly、neutral、中文或任何其它值。
- state=speak 时必须给非空 text；本地会创建新的语音文本，或直接替换当前尚未播放/正在播放的文本事务。
- state=wait 时不新增、不替换；不得写用户可见 text。
- 不要高频修改：只有新增回答、实质语义修正或必要补充时才用 speak；不要为轻微措辞变化反复替换同一段话。
- actions 是可选业务动作数组；本实验没有可用业务动作，所以必须是空数组。
- thinking/reasoning 可以使用，但不能代替最终 JSON 状态声明。
- 当前用户讲话时可以 state=speak 准备文本候选；系统只保存文本，用户结束讲话后才会启动 TTS。
"""


def context(*, ledger: SessionLedger, text: str, speaking: bool, sentence_id: int,
            snapshot: dict[str, Any], event: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": ENVELOPE_PROMPT},
        *ledger.context_messages(text),
        {"role": "system", "content": "调度状态：" + json.dumps({
            "now": time.time(),
            "events": [event],
            "user_speaking": speaking,
            "sentence": {"sentence_id": sentence_id, "revision": 1, "text": text},
            "speak_snapshot": snapshot,
            "text_only_queue": True,
        }, ensure_ascii=False)},
    ]


async def invoke(*, client: Any, base_url: str, model: str, api_key: str,
                 messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }
    result: dict[str, Any] = {
        "http_status": None, "finish_reason": None, "content": "", "reasoning": "",
        "sse_order": [], "error": None,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    try:
        async with client.stream("POST", f"{base_url}/chat/completions", headers=headers, json=payload) as response:
            result["http_status"] = response.status_code
            if response.status_code >= 400:
                result["error"] = (await response.aread()).decode("utf-8", errors="replace")[:500]
                return result
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                choices = item.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                result["finish_reason"] = choice.get("finish_reason") or result["finish_reason"]
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    result["reasoning"] += reasoning
                    result["sse_order"].append("reasoning")
                content = delta.get("content")
                if isinstance(content, str) and content:
                    result["content"] += content
                    result["sse_order"].append("content")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return result


def validate(result: dict[str, Any]) -> dict[str, Any]:
    """Validate the public response envelope without retaining generated text."""

    if result.get("http_status") != 200:
        return {"valid": False, "error": result.get("error") or "provider failure"}
    try:
        value = json.loads(result.get("content") or "")
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": f"invalid JSON: {str(exc)[:180]}"}
    if not isinstance(value, dict) or set(value) != {"speak", "actions"}:
        return {"valid": False, "error": "envelope must contain exactly speak and actions"}
    decision = value.get("speak")
    if not isinstance(decision, dict) or set(decision) - {"state", "emotion", "text"}:
        return {"valid": False, "error": "invalid speak object"}
    state = decision.get("state")
    if state not in {"speak", "wait"}:
        return {"valid": False, "error": "state must be speak or wait"}
    emotion = decision.get("emotion", "calm")
    if emotion not in EMOTIONS:
        return {"valid": False, "error": "invalid emotion"}
    text = decision.get("text", "")
    if not isinstance(text, str):
        return {"valid": False, "error": "text must be a string"}
    if state == "speak" and not text.strip():
        return {"valid": False, "error": "speak requires nonempty text"}
    if state == "wait" and "text" in decision and text.strip():
        return {"valid": False, "error": "wait cannot contain user-visible text"}
    if value.get("actions") != []:
        return {"valid": False, "error": "actions must be empty for this test"}
    return {"valid": True, "state": state, "emotion": emotion, "text": text,
            "text_chars": len(text)}


def public(result: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "http_status": result.get("http_status"),
        "finish_reason": result.get("finish_reason"),
        "content_chars": len(result.get("content") or ""),
        "reasoning_chars": len(result.get("reasoning") or ""),
        "sse_order": result.get("sse_order"),
        "provider_error": result.get("error"),
        "envelope_valid": decision.get("valid"),
        "state": decision.get("state"),
        "emotion": decision.get("emotion"),
        "text_chars": decision.get("text_chars"),
        "validation_error": decision.get("error"),
    }


async def one_run(*, client: Any, base_url: str, model: str, api_key: str,
                  max_tokens: int) -> dict[str, Any]:
    ledger = SessionLedger()
    events: list[dict[str, Any]] = []
    first = await invoke(
        client=client, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens,
        messages=context(ledger=ledger, text=FIRST_TEXT, speaking=True, sentence_id=1,
                         snapshot={"status": "IDLE", "text_only_queue": True}, event="ASR_INCREMENT"),
    )
    first_decision = validate(first)
    events.append({"event": "ASR_INCREMENT", "scheduler_action": "LLM_DISPATCHED",
                   "response": public(first, first_decision)})

    pending = first_decision.get("text", "") if first_decision.get("state") == "speak" else ""
    ledger.commit_input("synthetic_turn_1", FIRST_TEXT)
    events.append({"event": "ASR_FINAL_SAME_REVISION", "scheduler_action": "SKIPPED_FINALIZATION_ONLY",
                   "reason": "unchanged text already covered by local speech candidate"})
    if pending:
        ledger.commit_delivery("synthetic_speech_1", pending, DeliveryReport(
            status="completed", termination="completed", heard_text=pending, precision="complete",
        ))

    second = await invoke(
        client=client, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens,
        messages=context(ledger=ledger, text=SECOND_TEXT, speaking=True, sentence_id=2,
                         snapshot={"status": "IDLE", "previous_delivery_chars": len(pending),
                                   "last_action": "previous_speak_delivered", "text_only_queue": True},
                         event="ASR_INCREMENT"),
    )
    second_decision = validate(second)
    events.append({"event": "ASR_INCREMENT_NEW_SENTENCE", "scheduler_action": "LLM_DISPATCHED",
                   "response": public(second, second_decision)})
    return {
        "events": events,
        "assertions": {
            "first_required_speak_envelope_valid": bool(first_decision.get("valid")),
            "unchanged_final_did_not_dispatch": True,
            "second_required_speak_envelope_valid": bool(second_decision.get("valid")),
            "tts_or_pcm_started": False,
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    config = load_config((REPO_ROOT / args.config).resolve())
    api_key = config.api_key(config.llm.api_key_env)
    if not api_key:
        raise RuntimeError(f"未配置 {config.llm.api_key_env}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout))) as client:
        runs = []
        for _ in range(args.attempts):
            runs.append(await one_run(client=client, base_url=config.llm.base_url.rstrip("/"),
                                      model=config.llm.model, api_key=api_key, max_tokens=args.max_tokens))
    return {
        "probe": "speech-decision-envelope-e2e",
        "model": config.llm.model,
        "transport": "thinking + response_format=json_object; no native tools",
        "attempts": args.attempts,
        "runs": runs,
        "summary": {
            "first_valid": sum(item["assertions"]["first_required_speak_envelope_valid"] for item in runs),
            "second_valid": sum(item["assertions"]["second_required_speak_envelope_valid"] for item in runs),
            "final_skipped": sum(item["assertions"]["unchanged_final_did_not_dispatch"] for item in runs),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--attempts", type=int, default=5, choices=range(1, 16), metavar="1..15")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
