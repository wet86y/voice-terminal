"""Call DeepSeek against the proposed three-state speech decision envelope.

This local diagnostic substitutes for ASR post-text and the SpeechHandle
snapshot.  It deliberately never creates TTS work, PCM, or playback.  Each
case is sent to the real configured DeepSeek API in thinking mode with no
native tools; the response must be one JSON decision envelope.

Only protocol metadata, state values, and text lengths are saved.  Generated
speech text and reasoning are retained only transiently for validation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probe_speech_decision_envelope_e2e import invoke
from voice_terminal.config import load_config


EMOTIONS = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}

PROMPT = """你是持续监听的中文语音 Agent。每轮最终响应都是本地语音系统的必填状态声明，
不是普通聊天回答，也不是可选工具调用。

最终 content 必须且只能是一个 JSON 对象，严格使用：
{"speak":{"state":"speak、cancel、wait 之一","emotion":"可选","text":"仅 speak 时必填"},"actions":[]}

状态语义：
- speak：你现在要让用户听到 text。text 必须非空；本地会新建或替换文本语音队列。
- cancel：你认为当前不应回应用户。本地挂起现有待播文本但不删除它；cancel 不得带 text。
- wait：你不需要改变现有语音队列；wait 不得带 text。
- 若 speak_snapshot.queue_status=speaking，wait/cancel 都不打断已锁定播放；只有 speak(text) 才请求本地进入受控替换。
- 不要高频微调：只有新增回答、实质语义修正或必要补充才用 speak(text)，不能因轻微措辞变化反复替换文本。
- emotion 可省略；若提供，只能是 calm、happy、sad、angry、fearful、disgusted、surprised 中的一个英文值。
- actions 必须是空数组；本测试没有可用业务动作。
- 禁止 JSON 外的解释、Markdown 或普通正文。thinking/reasoning 可以使用，但不能替代最终 JSON。
"""


CASES: tuple[dict[str, Any], ...] = (
    {
        "name": "empty_to_speak",
        "expected": {"speak"},
        "snapshot": {"queue_status": "empty", "text_only_queue": True},
        "user": "你好，请简短向我问好。",
        "description": "无内容时，用户明确需要回应。",
    },
    {
        "name": "queued_to_cancel",
        "expected": {"cancel"},
        "snapshot": {"queue_status": "queued", "queued_text": "好的，我正在为你整理答案。", "text_only_queue": True},
        "user": "先别回答，等我说完再说。请挂起刚才准备说的内容。",
        "description": "排队中时，用户明确要求暂不回应。",
    },
    {
        "name": "suspended_to_speak",
        "expected": {"speak"},
        "snapshot": {"queue_status": "suspended", "queued_text": "旧的挂起候选。", "text_only_queue": True},
        "user": "现在可以回答了。请简短告诉我你听到了什么。",
        "description": "已挂起时，用户明确恢复回应；新文本应覆盖旧候选。",
    },
    {
        "name": "queued_to_wait",
        "expected": {"wait"},
        "snapshot": {"queue_status": "queued", "queued_text": "已经排队的完整回答。", "text_only_queue": True},
        "user": "不需要修改已经排队的回答，保持它，等我下一句话。",
        "description": "排队中且无需改动时，保持原候选。",
    },
    {
        "name": "speaking_to_wait",
        "expected": {"wait"},
        "snapshot": {
            "queue_status": "speaking", "active_text": "正在向用户说出的回答。",
            "heard_text": "正在向用户", "unheard_text": "说出的回答。", "text_only_queue": True,
        },
        "user": "没事，继续说，不需要改动。",
        "description": "讲话中且无需改动时，不能打断。",
    },
    {
        "name": "speaking_to_speak_replace",
        "expected": {"speak"},
        "snapshot": {
            "queue_status": "speaking", "active_text": "旧回答正在播放。",
            "heard_text": "旧回答", "unheard_text": "正在播放。", "text_only_queue": True,
        },
        "user": "请立刻修正：我刚才说的是明天，不是今天。请重新简短回答。",
        "description": "讲话中出现实质语义修正；speak(text) 请求受控替换。",
    },
)


def build_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": str(case["user"])},
        {"role": "system", "content": "本轮本地快照：" + json.dumps({
            "now": time.time(),
            "event": "SYNTHETIC_POST_ASR_INPUT",
            "user_speaking": False,
            "speak_snapshot": case["snapshot"],
            "scenario": case["description"],
        }, ensure_ascii=False)},
    ]


def validate(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("http_status") != 200:
        return {"valid": False, "error": result.get("error") or "provider failure"}
    try:
        value = json.loads(result.get("content") or "")
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": f"invalid JSON: {str(exc)[:180]}"}
    if not isinstance(value, dict) or set(value) != {"speak", "actions"}:
        return {"valid": False, "error": "envelope must contain exactly speak and actions"}
    speak = value.get("speak")
    if not isinstance(speak, dict) or set(speak) - {"state", "emotion", "text"}:
        return {"valid": False, "error": "invalid speak object"}
    state = speak.get("state")
    if state not in {"speak", "cancel", "wait"}:
        return {"valid": False, "error": "invalid speech state"}
    emotion = speak.get("emotion", "calm")
    if emotion not in EMOTIONS:
        return {"valid": False, "error": "invalid emotion"}
    text = speak.get("text", "")
    if not isinstance(text, str):
        return {"valid": False, "error": "text must be a string"}
    if state == "speak" and not text.strip():
        return {"valid": False, "error": "speak requires nonempty text"}
    if state in {"cancel", "wait"} and "text" in speak:
        return {"valid": False, "error": "cancel/wait must omit text"}
    if value.get("actions") != []:
        return {"valid": False, "error": "actions must be empty"}
    return {"valid": True, "state": state, "emotion": emotion, "text_chars": len(text)}


def public(result: dict[str, Any], decision: dict[str, Any], expected: set[str]) -> dict[str, Any]:
    state = decision.get("state")
    return {
        "http_status": result.get("http_status"),
        "finish_reason": result.get("finish_reason"),
        "content_chars": len(result.get("content") or ""),
        "reasoning_chars": len(result.get("reasoning") or ""),
        "sse_order": result.get("sse_order"),
        "provider_error": result.get("error"),
        "envelope_valid": bool(decision.get("valid")),
        "state": state,
        "emotion": decision.get("emotion"),
        "text_chars": decision.get("text_chars"),
        "state_matches_expected": state in expected,
        "validation_error": decision.get("error"),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    config = load_config((REPO_ROOT / args.config).resolve())
    api_key = config.api_key(config.llm.api_key_env)
    if not api_key:
        raise RuntimeError(f"未配置 {config.llm.api_key_env}")
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout))) as client:
        for attempt in range(1, args.attempts + 1):
            for case in CASES:
                result = await invoke(
                    client=client, base_url=config.llm.base_url.rstrip("/"), model=config.llm.model,
                    api_key=api_key, messages=build_messages(case), max_tokens=args.max_tokens,
                )
                decision = validate(result)
                rows.append({"attempt": attempt, "case": case["name"], **public(result, decision, case["expected"])})
    summary: dict[str, Any] = {}
    for case in CASES:
        group = [row for row in rows if row["case"] == case["name"]]
        summary[case["name"]] = {
            "expected_states": sorted(case["expected"]),
            "samples": len(group),
            "valid_envelope": sum(row["envelope_valid"] for row in group),
            "expected_state": sum(row["state_matches_expected"] for row in group),
            "states": dict(Counter(str(row["state"]) for row in group)),
            "validation_errors": dict(Counter(str(row["validation_error"]) for row in group if row["validation_error"])),
        }
    return {
        "probe": "speech-three-state-matrix",
        "model": config.llm.model,
        "transport": "thinking + response_format=json_object; no native tools; no TTS/PCM",
        "attempts": args.attempts,
        "rows": rows,
        "summary": summary,
        "tts_or_pcm_started": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--attempts", type=int, default=4, choices=range(1, 11), metavar="1..10")
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
