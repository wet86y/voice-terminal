"""Metadata-only real OpenCode Go balanced-mode soak.

This is a diagnostic harness, not a product path.  It constructs the current
AgentEpisodeRuntime with the real Responses provider and the real ActionRunner
(filesystem tools under ``docs`` and optional MiniMax web search).  It enters
the Agent after a synthetic ASR final event and never starts TTS/audio.  Model
content, reasoning text, tool payloads, credentials, and full prompts are
intentionally not persisted; the report contains only hashes, lengths and
control-plane metadata.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.speculative_tools import ActionRunner
from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from voice_terminal.config import load_config
from voice_terminal.core.events import event
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.providers.minimax_search import MiniMaxSearchClient
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


SCENARIOS: dict[str, list[str]] = {
    "wake": [
        "hei claw，请告诉我今天适合做什么。", "hei claw，简短确认你听见了我。",
        "hei claw，介绍一下当前会话状态。", "hei claw，给我一句鼓励的话。",
        "hei claw，把回答控制在一句话。", "hei claw，请说明你能做哪些只读操作。",
        "hei claw，帮我梳理今天的优先事项。", "hei claw，先等待我继续说。",
        "hei claw，请用平静语气回答现在是否在线。", "hei claw，本轮只做最小必要回应。",
    ],
    "ordinary": [
        "什么是上下文缓存？请用两句话解释。", "为什么语音助手需要区分等待和说话？",
        "请比较同步与异步任务的一个优缺点。", "请给一个适合新手的 JSON 例子。",
        "把‘先确认，再执行’改写成更自然的中文。", "请解释 400 毫秒合并窗口的用途。",
        "请列出保持对话连续性的两个要点。", "请用一句话说明什么是幂等。",
        "请给出一个不涉及工具的天气询问示例。", "请简短总结本轮普通问答要求。",
    ],
    "interrupt": [
        "请先给出一个较完整的分步解释，说明语音播报如何处理用户插话。",
        "请详细说明一次长回答被打断后哪些内容可以算作已听见。",
        "假设回答很长，请规划一个可被中途抢占的播报方案。",
        "请解释播放状态、候选文本和用户新输入之间的关系。",
        "请用较多细节描述一次实质修正与普通补充的区别。",
        "请先组织一段完整回答，然后考虑用户突然开始讲话的情况。",
        "请分析语音输出在网络延迟下怎样避免重复播报。",
        "请说明取消、替换、追加三种播报动作的边界。",
        "请给出一段较长但仍然清晰的连续播报说明。",
        "请解释为什么新的 ASR final 不应取消正在进行的模型请求。",
    ],
    "waiting_tools": [
        "请读取 docs/architecture.md，概括其中关于运行时的要点。",
        "请在 docs 目录中搜索 websocket 相关文件并告诉我文件名。",
        "请读取 docs/context-and-tools.md，回答工具结果为何是不可信资料。",
        "请搜索 docs 中包含 protocol 的文档，然后给出一个摘要。",
        "请查找 docs/operations-and-quality.md 并说明质量指标。",
        "请在本地 docs 文件中查找 latency，返回最相关文件的要点。",
        "请读取 docs/websocket-protocol.md，说明事件消息的基本结构。",
        "请搜索当前网页上 OpenCode Go 的最新信息并标注来源。",
        "请查找并读取 docs/llm-protocol-baseline.json 的用途。",
        "请同时考虑本地文档和最新网页信息，回答 Responses 是否适合流式输出。",
    ],
    "balanced_thinking": [
        "这是一个需要权衡的设计问题：请判断何时值得额外思考，并先给出可播报结论。",
        "请分析严格 schema、工具等待和用户体验的冲突，必要时请求一次思考。",
        "请设计一个兼顾及时性与准确性的多轮语音回答策略。",
        "请判断下面问题是否需要额外推理：如何避免工具结果过期？",
        "请针对长上下文压缩提出一个有理由的建议，先播报结论。",
        "请比较快速回答和均衡回答的风险，并在确有必要时使用 request_thinking。",
        "请给出一次工具提前返回、两秒等待和用户打断同时发生时的处理方案。",
        "请分析如何维护已有播报的完整前缀，同时补充新事实。",
        "请判断是否应该为一个简单事实请求额外思考，并说明理由。",
        "请综合前面讨论，给出一个稳定且不过度思考的协议建议。",
    ],
}


class _Handle:
    def __init__(self, owner: "_Output", speech_id: str, turn_id: str) -> None:
        self.owner, self.speech_id, self.turn_id = owner, speech_id, turn_id
        self.workspace = SimpleNamespace(timings={})
        self._parts: list[str] = []

    async def feed(self, text: str) -> None:
        self._parts.append(text)

    async def finish(self) -> None:
        text = "".join(self._parts)
        if text:
            self.owner.text = text if self.owner.text == "" else text
            self.owner.speak_snapshot.update({"text": self.owner.text, "queue_status": "queued"})
        self.owner.active = False


class _Output:
    """Text-only queue double: all actual TTS/network playback is disabled."""

    def __init__(self) -> None:
        self.active = False
        self.status = "IDLE"
        self.text = ""
        self.counter = 0
        self.cancel_count = 0
        self.replacements = 0
        self.appends = 0
        self.speak_snapshot = {"queue_status": "empty", "text_only_queue": True, "text": ""}

    def _new(self, turn_id: str) -> _Handle:
        self.counter += 1
        return _Handle(self, f"diag-speech-{self.counter}", turn_id)

    async def begin(self, _identity, turn_id, *, emotion="calm") -> _Handle:
        self.text = ""
        self.speak_snapshot.update({"queue_status": "queued", "speech_id": f"diag-speech-{self.counter+1}", "text": ""})
        return self._new(turn_id)

    async def replace_current(self, _identity, turn_id, *, emotion="calm") -> _Handle:
        self.replacements += 1
        self.text = ""
        self.speak_snapshot.update({"queue_status": "queued", "speech_id": f"diag-speech-{self.counter+1}", "text": ""})
        return self._new(turn_id)

    async def append_successor(self, _identity, turn_id, *, suffix, **_kwargs) -> _Handle:
        self.appends += 1
        handle = self._new(turn_id)
        handle._parts.append(str(suffix))
        return handle

    def cancel_all(self, _reason: str) -> None:
        self.cancel_count += 1
        self.active = False
        self.speak_snapshot.update({"queue_status": "empty", "text": ""})
        self.text = ""

    def cancel(self, _speech_id, _reason: str) -> None:
        self.cancel_all(_reason)


class _CatalogView:
    """Expose the real ActionRunner while optionally hiding tool schemas.

    The no-tool lanes still use the real runner object for lifecycle and
    action dispatch.  Hiding its catalog is useful as a control condition:
    OpenCode Go's strict Responses compiler rejects the current product
    schema when the merged action argument properties are present.
    """

    def __init__(self, runner: ActionRunner, *, expose_tools: bool) -> None:
        self.runner = runner
        self.expose_tools = expose_tools

    def action_definitions(self):
        return self.runner.action_definitions() if self.expose_tools else []

    def __getattr__(self, name):
        return getattr(self.runner, name)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    low = text.lower()
    if "connect" in low or "tls" in low:
        kind = "connect_error"
    elif "timeout" in low:
        kind = "timeout"
    elif "http" in low:
        kind = "http_error"
    elif "schema" in low or "envelope" in low or "action" in low:
        kind = "protocol_error"
    else:
        kind = type(exc).__name__
    return kind


def _event_summary(trace: list[dict]) -> dict[str, int]:
    return dict(Counter(str(item.get("kind")) for item in trace if item.get("kind")))


def _rows_for_trace(trace: list[dict], request_before: int, started: float, *, turn_id: str, scenario: str,
                    prompt: str, interrupted: bool) -> dict:
    relevant = trace[request_before:]
    req = [item for item in relevant if item.get("kind") == "LLM_DISPATCHED"]
    errors = [item for item in relevant if item.get("kind") in {"AGENT_ERROR", "LLM_PROVIDER_UNAVAILABLE", "LLM_OUTPUT_FILTERED", "SPEAK_ENVELOPE_INVALID", "LLM_EMPTY_OUTPUT", "TOOL_PROTOCOL_ERROR", "RESPONSES_INCOMPLETE"}]
    states = [item.get("state") for item in relevant if item.get("kind") == "SPEAK_STATE_CHANGED" and item.get("state")]
    latency = round((time.monotonic() - started) * 1000, 1)
    return {
        "scenario": scenario, "turn_id_hash": _digest(turn_id), "input_sha256": _digest(prompt),
        "input_chars": len(prompt), "interrupted": interrupted, "requests_dispatched": len(req),
        "states": states[-8:], "request_thinking": sum(item.get("action_count", 0) and 1 or 0 for item in relevant if item.get("kind") == "THINKING_REQUESTED"),
        "thinking_barrier_ready": sum(1 for item in relevant if item.get("kind") == "THINKING_BARRIER_READY"),
        "thinking_barrier_timed_out": sum(bool(item.get("timed_out")) for item in relevant if item.get("kind") == "THINKING_BARRIER_READY"),
        "actions_started": sum(1 for item in relevant if item.get("kind") == "ACTION_STARTED"),
        "actions_completed": sum(1 for item in relevant if item.get("kind") == "ACTION_COMPLETED"),
        "tool_names": sorted({str(item.get("name")) for item in relevant if item.get("kind") == "ACTION_STARTED" and item.get("name")} ),
        "errors": sorted({_safe_error(RuntimeError(str(item.get("error") or item.get("code") or item.get("reason") or "error"))) for item in errors}),
        "event_counts": _event_summary(relevant), "elapsed_ms": latency,
    }


async def _wait_quiet(runtime: AgentEpisodeRuntime, *, max_seconds: float = 8.0) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        tasks = [task for task in runtime._background_tools if not task.done()]
        runner = runtime.runner
        active = runtime.active_request
        if not tasks and (runner is None or runner.done()) and (active is None or active.done()):
            return
        await asyncio.sleep(0.05)


async def _scenario(name: str, prompts: list[str], config, sem: asyncio.Semaphore) -> list[dict]:
    conversation = ConversationRuntime()
    ledger = SessionLedger()
    files = FileTools((ROOT / "docs",))
    search = None
    minimax_key = config.api_key(config.tts.api_key_env)
    if minimax_key and config.tools.web_search:
        search = WebSearchTools(MiniMaxSearchClient(base_url=config.tts.base_url, api_key=minimax_key, timeout_ms=5000))
    real_registry = ActionRunner(files, search=search)
    # Tool-specific lanes expose the actual schemas.  Other lanes remain a
    # strict-schema control condition while retaining the ActionRunner below.
    registry = _CatalogView(real_registry, expose_tools=name == "waiting_tools")
    provider = DeepSeekClient(
        base_url=config.llm.base_url, model=config.llm.model, api_key=config.api_key(config.llm.api_key_env),
        provider="opencode_go", api_key_env=config.llm.api_key_env, session_id=f"balanced-soak-{uuid4().hex}",
        timeout_ms=max(1000, config.llm.timeout_ms), transport="responses", thinking="disabled",
        reasoning_effort="low", max_output_tokens=min(768, config.llm.max_output_tokens),
        protocol_recovery_limit=1, transport_retry_limit=1,
    )
    output = _Output()
    events: list[dict] = []
    async def emit(kind, payload):
        # Keep only event names and numeric/enum control-plane fields in memory
        # for the report.  The live runtime itself still receives its payload.
        if kind == "agent.trace" and isinstance(payload, dict):
            events.append({key: payload.get(key) for key in ("kind", "state", "name", "timed_out", "reason", "code", "status", "action_count", "terminal_kind") if key in payload})
        elif kind in {"error", "speech.append_waiting_prefix"} and isinstance(payload, dict):
            events.append({"kind": kind, "code": payload.get("code"), "status": payload.get("status")})
    runtime = AgentEpisodeRuntime(provider, registry, output, emit, conversation=conversation, ledger=ledger,
                                  options=AgentOptions(enabled=True, scheduler_tick_ms=400, trace_limit=300, output_limit=32),
                                  tools_enabled=True, reasoning_mode="balanced")
    rows: list[dict] = []
    try:
        await runtime.ensure_episode()
        for round_no, prompt in enumerate(prompts, 1):
            turn_id = f"{name}-{round_no}-{uuid4().hex[:8]}"
            before_trace = len(runtime.trace)
            started = time.monotonic()
            interrupted = name == "interrupt"
            # Synthetic ASR final: this is the same Agent observation emitted
            # by the controller after endpoint/ASR finalization.
            runtime.last_turn_id = turn_id
            runtime.inputs[turn_id] = {"text": prompt, "final": True, "input_revision": 1}
            identity = (runtime.episode_id, f"diag-{uuid4().hex}")
            runtime.agent_step_id = identity[1]
            runtime._identity_turns[identity] = turn_id
            async with sem:
                if interrupted:
                    request = asyncio.create_task(runtime._step(identity, {"USER_INPUT_FINALIZED"}), name=f"soak-{name}-{round_no}")
                    runtime.active_request = request
                    await asyncio.sleep(0.06)
                    runtime.cancel_active_request("user_interrupted")
                    output.cancel_all("interrupted")
                    try:
                        await request
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        await runtime.log("AGENT_ERROR", identity=identity, turn_id=turn_id,
                                          error=_safe_error(exc))
                    await runtime.log("SPEECH_STARTED", identity=identity, turn_id=turn_id)
                    await runtime.log("BARGE_IN_CONFIRMED", identity=identity, turn_id=turn_id)
                else:
                    try:
                        await runtime._step(identity, {"USER_INPUT_FINALIZED"})
                    except Exception as exc:
                        # The provider's full error body is deliberately not
                        # retained; event metadata is enough for diagnosis.
                        await runtime.log("AGENT_ERROR", identity=identity, turn_id=turn_id,
                                          error=_safe_error(exc))
            await _wait_quiet(runtime)
            new_trace = list(runtime.trace)
            rows.append(_rows_for_trace(new_trace, before_trace, started, turn_id=turn_id, scenario=name,
                                         prompt=prompt, interrupted=interrupted))
            await asyncio.sleep(0.02)
    finally:
        runtime.closed = True
        runtime.cancel_active_request("diagnostic_end")
        for task in list(runtime._background_tools):
            task.cancel()
        real_registry.close()
        await provider.close()
    return rows


async def _probe_real_tools(config) -> dict:
    """Exercise read-only business adapters once, without retaining results."""
    key = config.api_key(config.tts.api_key_env)
    search = None
    if key and config.tools.web_search:
        search = WebSearchTools(MiniMaxSearchClient(base_url=config.tts.base_url, api_key=key, timeout_ms=5000))
    runner = ActionRunner(FileTools((ROOT / "docs",)), search=search)
    turn = f"tool-probe-{uuid4().hex}"
    runner.begin_turn(turn)
    rows = []
    try:
        for name, args in (("files_search", {"query": "architecture"}), ("web_search", {"query": "OpenCode Go"})):
            if name == "web_search" and search is None:
                rows.append({"name": name, "status": "DISABLED", "latency_ms": 0.0})
                continue
            started = time.monotonic()
            try:
                entry, reused = await runner.select(turn, name, args)
                result = await asyncio.shield(entry.task)
                rows.append({"name": name, "status": "completed", "reused": bool(reused),
                             "result_shape": sorted(result.keys()) if isinstance(result, dict) else type(result).__name__,
                             "latency_ms": round((time.monotonic() - started) * 1000, 1)})
            except Exception as exc:
                rows.append({"name": name, "status": "error", "error": _safe_error(exc),
                             "latency_ms": round((time.monotonic() - started) * 1000, 1)})
    finally:
        runner.close()
    return {"enabled": True, "rows": rows}


async def run(args: argparse.Namespace) -> dict:
    os.environ["VOICE_LLM_PROVIDER"] = "opencode_go"
    config = load_config(ROOT / "config" / "default.yaml", ROOT / "config" / "local.yaml")
    if config.llm.provider != "opencode_go":
        raise RuntimeError("provider override did not select opencode_go")
    key = config.api_key(config.llm.api_key_env)
    if not key:
        raise RuntimeError(f"missing {config.llm.api_key_env}")
    preflight = {"provider": config.llm.provider, "base_url": config.llm.base_url, "model": config.llm.model,
                 "transport": "responses", "credential_present": True, "models_status": None,
                 "model_available": None, "web_search_credential_present": bool(config.api_key(config.tts.api_key_env))}
    pre_client = DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model, api_key=key,
                                provider="opencode_go", api_key_env=config.llm.api_key_env, transport="responses",
                                thinking="disabled", reasoning_effort="low", timeout_ms=15000)
    try:
        response = await (await pre_client._http_client()).get(f"{config.llm.base_url}/models", headers=pre_client._request_headers())
        preflight["models_status"] = int(response.status_code)
        if response.status_code < 400:
            try:
                body = response.json(); rows = body.get("data", body) if isinstance(body, dict) else body
                ids = {str(row.get("id")) for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
                preflight["model_available"] = config.llm.model in ids
            except Exception:
                preflight["model_available"] = False
    except Exception as exc:
        preflight["models_error"] = _safe_error(exc)
    finally:
        await pre_client.close()
    if preflight["models_status"] is None or preflight["models_status"] >= 400:
        return {"probe": "balanced-mode-agent-actionrunner-soak", "preflight": preflight, "blocked": True,
                "blocked_reason": "provider_models_unavailable", "scenarios": [], "rows": []}
    tool_probe = await _probe_real_tools(config)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    batches = await asyncio.gather(*[_scenario(name, prompts, config, sem) for name, prompts in SCENARIOS.items()])
    rows = [row for batch in batches for row in batch]
    summary = {
        "scenario_count": len(SCENARIOS), "rounds_per_scenario": {name: len(prompts) for name, prompts in SCENARIOS.items()},
        "total_rounds": len(rows), "valid_or_completed_rounds": sum(not row["errors"] and row["requests_dispatched"] > 0 for row in rows),
        "rounds_with_errors": sum(bool(row["errors"]) for row in rows),
        "request_thinking_rounds": sum(row["request_thinking"] for row in rows),
        "thinking_barrier_ready": sum(row["thinking_barrier_ready"] for row in rows),
        "thinking_barrier_timeouts": sum(row["thinking_barrier_timed_out"] for row in rows),
        "action_started": sum(row["actions_started"] for row in rows), "action_completed": sum(row["actions_completed"] for row in rows),
        "tool_names": dict(Counter(name for row in rows for name in row["tool_names"])),
        "interrupted_rounds": sum(row["interrupted"] for row in rows),
        "latency_ms": {"min": min(row["elapsed_ms"] for row in rows), "p50": statistics.median(row["elapsed_ms"] for row in rows), "max": max(row["elapsed_ms"] for row in rows)},
        "errors": dict(Counter(error for row in rows for error in row["errors"])),
        # Observed in the provider response body during the first failed
        # request; retain only a stable, non-content signature.
        "provider_error_signatures": [
            "HTTP_400_required_properties_must_match_all_properties_in_object"
        ] if any(row["errors"] for row in rows) else [],
    }
    return {"probe": "balanced-mode-agent-actionrunner-soak", "provider": "opencode_go", "transport": "responses",
            "reasoning_mode": "balanced", "asr_stage": "synthetic_final_after_asr_before_tts", "tts_started": False,
            "real_actionrunner": True, "real_files_tool": True, "real_web_search_tool": bool(preflight["web_search_credential_present"]),
            "preflight": preflight, "tool_probe": tool_probe, "blocked": False, "summary": summary, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "blocked": report.get("blocked"), "summary": report.get("summary"), "preflight": report.get("preflight")}, ensure_ascii=False, indent=2))
    return 0 if not report.get("blocked") else 2


if __name__ == "__main__":
    raise SystemExit(main())
