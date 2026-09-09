"""Broad real-API diagnostics for the three-state speech response envelope.

This probe substitutes for post-ASR input and the local speech snapshot.  It
uses DeepSeek thinking mode with response_format=json_object and no native
tools.  It never starts TTS or creates PCM.  Reports retain only metadata,
lengths, states, and validation errors; generated text/reasoning is discarded.
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

PROMPT = """你是持续监听的中文语音 Agent。每轮最终响应都是本地语音系统的必填状态声明。
最终 content 必须且只能是 JSON 对象：
{"speak":{"state":"speak|cancel|wait","emotion":"可选","text":"仅 speak 必填"},"actions":[]}

严格规则：
- speak(text)：需要让用户听到新文本；无内容时创建队列，排队中或已挂起时填充/替换，讲话中时表示实质修正并请求受控替换。text 非空。
- cancel：当前不应该回应用户；挂起已有待播文本但不删除。不得带 text。讲话中时不能打断已锁定播放。
- wait：不修改当前说话队列，保持上一轮状态，等待下一轮判断。不得带 text。
- 如果只是连续 ASR 增量、轻微错字、尚未完成的句子或没有新语义，优先 wait；禁止因轻微措辞变化高频 speak 替换。
- queue_status 的 speaking 状态下，只有用户给出实质语义修正才用 speak；仅“继续/不用改”用 wait；要求暂不响应时用 cancel，但不能打断当前已锁定播放。
- actions 必须始终是空数组；本测试不提供业务工具。
- emotion 省略或只能是 calm、happy、sad、angry、fearful、disgusted、surprised。
- 禁止 JSON 外解释、Markdown、普通正文。不要把 reasoning 当最终响应。
"""

CASES: tuple[dict[str, Any], ...] = (
    {"name": "empty_explicit_reply", "expected": {"speak"}, "snapshot": {"queue_status": "empty", "text_only_queue": True}, "user": "请简短回答：你能听见我吗？", "kind": "state"},
    {"name": "empty_insufficient_two_utterances", "expected": {"wait"}, "snapshot": {"queue_status": "empty", "text_only_queue": True}, "user": "我先说半句，等我下一句补充。", "kind": "state"},
    {"name": "queued_cancel_suspend", "expected": {"cancel"}, "snapshot": {"queue_status": "queued", "queued_text_chars": 15, "text_only_queue": True}, "user": "先不要回答，挂起刚才排队的内容，等我说完。", "kind": "state"},
    {"name": "suspended_resume_speak", "expected": {"speak"}, "snapshot": {"queue_status": "suspended", "queued_text_chars": 15, "text_only_queue": True}, "user": "现在可以回答了，请用一句话告诉我你理解的重点。", "kind": "state"},
    {"name": "queued_no_change_wait", "expected": {"wait"}, "snapshot": {"queue_status": "queued", "queued_text_chars": 22, "text_only_queue": True}, "user": "保持已经排队的回答，不要修改，等我下一句话。", "kind": "state"},
    {"name": "asr_incremental_minor_revision", "expected": {"wait"}, "snapshot": {"queue_status": "queued", "queued_text_chars": 20, "text_only_queue": True}, "user": "我想问一下明天的天气…（ASR 增量，句子尚未结束，只有轻微字词变化）", "kind": "incremental"},
    {"name": "speaking_continue_wait", "expected": {"wait"}, "snapshot": {"queue_status": "speaking", "active_text_chars": 18, "heard_text_chars": 8, "unheard_text_chars": 10, "text_only_queue": True}, "user": "继续说就好，不需要改动。", "kind": "state"},
    {"name": "speaking_cancel_no_interrupt", "expected": {"cancel"}, "snapshot": {"queue_status": "speaking", "active_text_chars": 18, "heard_text_chars": 8, "unheard_text_chars": 10, "text_only_queue": True}, "user": "我暂时不需要新的回应，但不要打断当前已经开始的播放。", "kind": "state"},
    {"name": "speaking_substantive_correction", "expected": {"speak"}, "snapshot": {"queue_status": "speaking", "active_text_chars": 18, "heard_text_chars": 8, "unheard_text_chars": 10, "text_only_queue": True}, "user": "重要修正：我说的是明天上午，不是今天下午，请重新简短回答。", "kind": "state"},
    {"name": "actions_reserved_empty", "expected": {"speak", "wait", "cancel"}, "snapshot": {"queue_status": "empty", "text_only_queue": True}, "user": "本轮只检查响应协议，先不要执行任何业务动作。", "kind": "contract"},
)


def messages(case: dict[str, Any], *, recovery: bool = False) -> list[dict[str, Any]]:
    event = "SYNTHETIC_ASR_INCREMENTAL" if case["kind"] == "incremental" else "SYNTHETIC_POST_ASR"
    if recovery:
        event = "RECOVERY_AFTER_INVALID_ENVELOPE"
    snapshot = dict(case["snapshot"])
    snapshot.update({"text_only_queue": True, "now": time.time()})
    if recovery:
        snapshot["protocol_error"] = "previous response envelope was invalid or incomplete; emit one complete JSON envelope now"
    return [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": str(case["user"])},
        {"role": "system", "content": "本轮调度快照：" + json.dumps({"event": event, "user_speaking": case["kind"] == "incremental", "speak_snapshot": snapshot}, ensure_ascii=False)},
    ]


def validate(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("http_status") != 200:
        return {"valid": False, "error": result.get("error") or "provider failure"}
    try:
        value = json.loads(result.get("content") or "")
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": f"invalid_json:{str(exc)[:120]}"}
    if not isinstance(value, dict) or set(value) != {"speak", "actions"}:
        return {"valid": False, "error": "envelope_keys"}
    speak = value.get("speak")
    if not isinstance(speak, dict) or set(speak) - {"state", "emotion", "text"}:
        return {"valid": False, "error": "speak_object"}
    state = speak.get("state")
    if state not in {"speak", "cancel", "wait"}:
        return {"valid": False, "error": "state"}
    if speak.get("emotion", "calm") not in EMOTIONS:
        return {"valid": False, "error": "emotion"}
    if value.get("actions") != []:
        return {"valid": False, "error": "actions_not_empty"}
    if state == "speak":
        text = speak.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"valid": False, "error": "speak_text_missing"}
        return {"valid": True, "state": state, "text_chars": len(text)}
    if "text" in speak:
        return {"valid": False, "error": "non_speak_has_text"}
    return {"valid": True, "state": state, "text_chars": 0}


def metadata(result: dict[str, Any], decision: dict[str, Any], expected: set[str]) -> dict[str, Any]:
    state = decision.get("state")
    return {"http_status": result.get("http_status"), "finish_reason": result.get("finish_reason"), "content_chars": len(result.get("content") or ""), "reasoning_chars": len(result.get("reasoning") or ""), "sse_order": result.get("sse_order"), "envelope_valid": bool(decision.get("valid")), "state": state, "text_chars": decision.get("text_chars"), "expected_states": sorted(expected), "state_matches": state in expected, "validation_error": decision.get("error"), "provider_error": result.get("error")}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import httpx
    config = load_config((REPO_ROOT / args.config).resolve())
    key = config.api_key(config.llm.api_key_env)
    if not key:
        raise RuntimeError(f"未配置 {config.llm.api_key_env}")
    rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout))) as client:
        for attempt in range(1, args.attempts + 1):
            for case in CASES:
                result = await invoke(client=client, base_url=config.llm.base_url.rstrip("/"), model=config.llm.model, api_key=key, messages=messages(case), max_tokens=args.max_tokens)
                decision = validate(result)
                rows.append({"attempt": attempt, "case": case["name"], **metadata(result, decision, case["expected"])})
        # The malformed response is synthetic by design; the recovery request is real API.
        malformed = '{"speak":{"state":"speak","text":"不完整'
        fake = {"http_status": 200, "finish_reason": "stop", "content": malformed, "reasoning": "", "sse_order": ["content"], "error": None}
        fake_decision = validate(fake)
        recovery_case = {"name": "synthetic_invalid_json_recovery", "expected": {"speak", "wait", "cancel"}, "snapshot": {"queue_status": "queued", "queued_text_chars": 12, "text_only_queue": True}, "user": "刚才的响应格式损坏，请保持当前队列并给出完整状态。", "kind": "state"}
        recovery = await invoke(client=client, base_url=config.llm.base_url.rstrip("/"), model=config.llm.model, api_key=key, messages=messages(recovery_case, recovery=True), max_tokens=args.max_tokens)
        recovery_decision = validate(recovery)
        recovery_rows.append({"malformed_detected": not fake_decision.get("valid", False), "malformed_error": fake_decision.get("error"), "recovery": metadata(recovery, recovery_decision, recovery_case["expected"])})
    summary = {}
    for case in CASES:
        group = [r for r in rows if r["case"] == case["name"]]
        summary[case["name"]] = {"samples": len(group), "valid": sum(r["envelope_valid"] for r in group), "state_matches": sum(r["state_matches"] for r in group), "states": dict(Counter(str(r["state"]) for r in group)), "errors": dict(Counter(str(r["validation_error"]) for r in group if r["validation_error"]))}
    return {"probe": "speech-envelope-broad", "model": config.llm.model, "transport": "real DeepSeek thinking + response_format=json_object; no native tools; no TTS/PCM", "attempts": args.attempts, "case_count": len(CASES), "rows": rows, "summary": summary, "recovery": recovery_rows, "tts_or_pcm_started": False}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--attempts", type=int, default=2, choices=range(1, 6))
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    report = asyncio.run(run(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
