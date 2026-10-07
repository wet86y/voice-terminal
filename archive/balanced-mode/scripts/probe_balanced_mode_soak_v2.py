"""Real OpenCode Go balanced-mode Agent/Responses/ActionRunner soak.

Diagnostic-only harness.  It deliberately retains control-plane metadata only:
no API credentials, model/reasoning/prompt/tool bodies, or URLs are written.
The synthetic input boundary is USER_INPUT_FINALIZED (after ASR final and
before TTS).  Six lanes x ten rounds cover wake, ordinary, interruption,
tool-waiting, and balanced request_thinking with/without business tools.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.speculative_tools import ActionRunner
from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from voice_terminal.config import load_config
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.providers.minimax_search import MiniMaxSearchClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane
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
        "把先确认再执行改写成更自然的中文。", "请解释 400 毫秒合并窗口的用途。",
        "请列出保持对话连续性的两个要点。", "请用一句话说明什么是幂等。",
        "请给出一个不涉及工具的天气询问示例。", "请简短总结本轮普通问答要求。",
    ],
    "interrupt": [
        "请先给出较完整的分步解释，说明语音播报如何处理用户插话。",
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
        "请读取 architecture.md，概括其中关于运行时的要点。",
        "请在允许的本地 docs 资料中搜索 websocket 相关文件并告诉我文件名。",
        "请读取 context-and-tools.md，回答工具结果为何是不可信资料。",
        "请搜索允许的本地 docs 资料中包含 protocol 的文档，然后给出摘要。",
        "请查找 operations-and-quality.md 并说明质量指标。",
        "请在允许的本地 docs 资料中查找 latency，返回最相关文件的要点。",
        "请读取 websocket-protocol.md，说明事件消息的基本结构。",
        "请查找并读取 llm-protocol-baseline.json 的用途。",
        "请搜索允许的本地 docs 资料中与 Responses 相关的文件并概括用途。",
        "请同时考虑允许的本地 docs 资料，回答 Responses 是否适合流式输出。",
    ],
    "balanced_no_tools": [
        "这是一个需要权衡的设计问题；若额外思考明显有益，请请求 request_thinking，并先给出可播报结论。",
        "请分析严格 schema 与用户体验的冲突；确有必要时使用 request_thinking，必须同步输出非空 speak。",
        "请设计兼顾及时性与准确性的多轮语音回答策略；必要时请求一次 request_thinking。",
        "请判断如何避免工具结果过期；若需要额外推理，请使用 request_thinking。",
        "请针对长上下文压缩提出有理由的建议；必要时请求 request_thinking。",
        "请比较快速回答和均衡回答的风险；确有必要时请求 request_thinking。",
        "请给出工具提前返回、两秒等待和用户打断同时发生时的处理方案；必要时请求思考。",
        "请分析如何维护已有播报的完整前缀，同时补充新事实；必要时请求思考。",
        "请判断简单事实请求是否值得额外思考，并说明理由；不要无依据循环。",
        "请综合前面讨论，给出稳定且不过度思考的协议建议；若确有必要才请求思考。",
    ],
    "balanced_with_tools": [
        "请实际读取 architecture.md 并概括要点；如工具结果需要权衡，请请求 request_thinking。",
        "请实际搜索允许的本地 docs 资料中的 websocket 文件并总结；必要时使用 request_thinking。",
        "请实际读取 context-and-tools.md 后判断工具资料可信边界，必要时请求思考。",
        "请实际搜索允许的本地 docs 资料中包含 protocol 的文件并给出摘要；需要权衡时请求思考。",
        "请实际读取 operations-and-quality.md 并说明指标；必要时请求 request_thinking。",
        "请实际搜索允许的本地 docs 资料中 latency 相关文件并回答；若需要额外推理请请求思考。",
        "请实际读取 websocket-protocol.md，判断流式输出取舍；必要时请求思考。",
        "请实际读取 llm-protocol-baseline.json 并说明用途；必要时请求 request_thinking。",
        "请实际搜索允许的本地 docs 资料中 Responses 相关文件并给出稳定建议；必要时请求思考。",
        "请实际读取 architecture.md 与 context-and-tools.md，综合回答并在必要时请求思考。",
    ],
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split()).lower()
    if isinstance(exc, FileNotFoundError) or "winerror 3" in text or "no such file" in text:
        return "file_not_found"
    if isinstance(exc, PermissionError):
        return "permission_error"
    if "timeout" in text:
        return "timeout"
    if "connect" in text or "tls" in text:
        return "connect_error"
    if re.search(r"\bhttp\s*[45]\d\d\b", text):
        return "http_error"
    if "schema" in text or "envelope" in text or "action" in text:
        return "protocol_error"
    if "incomplete" in text or "terminal" in text:
        return "stream_terminal"
    return type(exc).__name__


def http_status(exc: BaseException) -> int | None:
    match = re.search(r"HTTP\s*(\d{3})", str(exc), flags=re.I)
    return int(match.group(1)) if match else None


class OutputDouble:
    """Text queue double: records queue behavior while never starting TTS."""

    def __init__(self) -> None:
        self.status = "IDLE"
        self.active = False
        self.text = ""
        self.counter = 0
        self.cancel_count = 0
        self.replacements = 0
        self.appends = 0
        self.speak_snapshot = {"queue_status": "empty", "text_only_queue": True, "text": ""}

    def _handle(self, turn_id: str):
        self.counter += 1
        return OutputHandle(self, f"diag-speech-{self.counter}", turn_id)

    async def begin(self, _identity, turn_id, *, emotion="calm"):
        self.active = True
        self.text = ""
        self.speak_snapshot.update(queue_status="queued", text="", speech_id=f"diag-speech-{self.counter+1}")
        return self._handle(turn_id)

    async def replace_current(self, _identity, turn_id, *, emotion="calm"):
        self.active = True
        self.replacements += 1
        self.text = ""
        self.speak_snapshot.update(queue_status="queued", text="", speech_id=f"diag-speech-{self.counter+1}")
        return self._handle(turn_id)

    async def append_successor(self, _identity, turn_id, *, suffix, **_kwargs):
        self.appends += 1
        return self._handle(turn_id, prefix=str(suffix))

    def cancel_all(self, _reason: str) -> None:
        self.cancel_count += 1
        self.active = False
        self.text = ""
        self.speak_snapshot.update(queue_status="empty", text="")

    def cancel(self, _speech_id, reason: str) -> None:
        self.cancel_all(reason)

    async def close(self) -> None:
        self.cancel_all("close")


class OutputHandle:
    def __init__(self, owner: OutputDouble, speech_id: str, turn_id: str, prefix: str = "") -> None:
        self.owner, self.speech_id, self.turn_id = owner, speech_id, turn_id
        self.workspace = SimpleNamespace(timings={})
        self.parts = [prefix] if prefix else []

    async def feed(self, text: str) -> None:
        self.parts.append(text)

    async def finish(self) -> None:
        text = "".join(self.parts)
        if text:
            self.owner.text = text
            self.owner.speak_snapshot.update(text=text, queue_status="queued")
        self.owner.active = False


class CatalogView:
    def __init__(self, runner: ActionRunner, expose_tools: bool) -> None:
        self.runner, self.expose_tools = runner, expose_tools

    def action_definitions(self):
        return self.runner.action_definitions() if self.expose_tools else []

    def __getattr__(self, name):
        return getattr(self.runner, name)


class RecordingProvider:
    """Delegate to the real provider and retain only stream metadata."""

    def __init__(self, client: DeepSeekClient) -> None:
        self.client = client
        self.request_meta: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def stream_events(self, messages, **kwargs):
        meta = {"content_chars": 0, "reasoning_chars": 0, "finish": None, "events": 0, "error": None, "http_status": None}
        self.request_meta.append(meta)
        try:
            async for item in self.client.stream_events(messages, **kwargs):
                meta["events"] += 1
                kind = item.get("type")
                if kind == "content" and isinstance(item.get("text"), str):
                    meta["content_chars"] += len(item["text"])
                elif kind == "reasoning" and isinstance(item.get("text"), str):
                    meta["reasoning_chars"] += len(item["text"])
                elif kind == "finish":
                    meta["finish"] = item.get("reason")
                yield item
        except Exception as exc:
            meta["error"] = safe_error(exc)
            meta["http_status"] = http_status(exc)
            raise


async def wait_runtime(runtime: AgentEpisodeRuntime, seconds: float = 18.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        active = runtime.active_request
        delay = runtime.delay_task
        background = [task for task in runtime._background_tools if not task.done()]
        if (active is None or active.done()) and (delay is None or delay.done()) and not background:
            return
        await asyncio.sleep(0.03)


def trace_slice(runtime: AgentEpisodeRuntime, before: int) -> list[dict]:
    # The deque can contain at most trace_limit rows; this soak uses one lane
    # per episode and takes a small slice immediately after each round.
    rows = list(runtime.trace)
    return rows[before:]


def metadata_events(events: list[dict], before: int) -> list[dict]:
    return events[before:]


def first_at(rows: list[dict], kind: str) -> float | None:
    for row in rows:
        if row.get("kind") == kind and isinstance(row.get("at"), (int, float)):
            return float(row["at"])
    return None


async def run_lane(name: str, prompts: list[str], config, semaphore: asyncio.Semaphore, rounds: int) -> list[dict]:
    files = FileTools((ROOT / "docs",))
    search = None
    search_key = config.api_key(config.tts.api_key_env)
    if search_key and config.tools.web_search:
        search = WebSearchTools(MiniMaxSearchClient(base_url=config.tts.base_url, api_key=search_key, timeout_ms=5000))
    real_runner = ActionRunner(files, search=search)
    registry = CatalogView(real_runner, expose_tools=name in {"waiting_tools", "balanced_with_tools"})
    client = DeepSeekClient(
        base_url=config.llm.base_url, model=config.llm.model,
        api_key=config.api_key(config.llm.api_key_env), provider="opencode_go",
        api_key_env=config.llm.api_key_env, session_id=f"balanced-v2-{uuid4().hex}",
        timeout_ms=max(1000, config.llm.timeout_ms), transport="responses", thinking="disabled",
        reasoning_effort="low", max_output_tokens=min(768, config.llm.max_output_tokens),
        protocol_recovery_limit=1, transport_retry_limit=1,
    )
    provider = RecordingProvider(client)
    output = OutputDouble()
    trace_events: list[dict] = []

    async def emit(kind, payload):
        if kind == "agent.trace" and isinstance(payload, dict):
            keep = ("kind", "state", "name", "timed_out", "reason", "code", "status", "action_count", "event", "pending_count", "business_action_count", "generation", "terminal_kind")
            trace_events.append({key: payload.get(key) for key in keep if key in payload})
        elif kind == "speech.append_waiting_prefix" and isinstance(payload, dict):
            trace_events.append({"kind": kind, "baseline_chars": payload.get("baseline_chars"), "received_chars": payload.get("received_chars")})
        elif kind == "speak.preview" and isinstance(payload, dict):
            trace_events.append({"kind": kind, "state": payload.get("state"), "status": payload.get("status"),
                                 "committed": payload.get("committed"), "text_chars": len(str(payload.get("text") or "")),
                                 "text_delta_chars": len(str(payload.get("text_delta") or ""))})

    runtime = AgentEpisodeRuntime(
        provider, registry, output, emit, conversation=ConversationRuntime(), ledger=SessionLedger(),
        options=AgentOptions(enabled=True, scheduler_tick_ms=400, trace_limit=300, output_limit=32),
        tools_enabled=True, reasoning_mode="balanced",
    )
    rows: list[dict] = []
    try:
        await runtime.ensure_episode()
        for round_no, prompt in enumerate(prompts[:rounds], 1):
            turn_id = f"{name}-{round_no}-{uuid4().hex[:8]}"
            trace_before = len(runtime.trace)
            event_before = len(trace_events)
            provider_before = len(provider.request_meta)
            started = time.monotonic()
            cancellation_triggered = False
            cancellation_latency_ms = None
            if name == "wake":
                await runtime.observe("WAKE_DETECTED", turn_id=turn_id, text="wake")
            await runtime.observe(
                "USER_INPUT_FINALIZED", turn_id=turn_id, text=prompt,
                input_finished_at=time.time(), source="synthetic_asr_final",
            )
            if name == "interrupt":
                # Wait for a real provider request (or its 400ms gate), then
                # model user speech onset and verify explicit cancellation.
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and runtime.active_request is None:
                    await asyncio.sleep(0.01)
                cancel_started = time.monotonic()
                await runtime.speech_activity(True)
                cancellation_triggered = True
                while runtime.active_request is not None and not runtime.active_request.done() and time.monotonic() - cancel_started < 2:
                    await asyncio.sleep(0.01)
                cancellation_latency_ms = round((time.monotonic() - cancel_started) * 1000, 1)
            if name in {"balanced_no_tools", "balanced_with_tools"}:
                # If the real model asks for thinking, inject speech onset
                # while the local barrier is live; stale-generation handling
                # then proves that user speech does not wait for its deadline.
                deadline = time.monotonic() + 16.0
                requested = False
                while time.monotonic() < deadline:
                    if any(item.get("kind") == "THINKING_REQUESTED" for item in list(runtime.trace)[trace_before:]):
                        requested = True
                        break
                    if runtime.active_request is None and runtime.request_count > 0 and not runtime._background_tools:
                        break
                    await asyncio.sleep(0.02)
                if requested and runtime._background_tools:
                    cancel_started = time.monotonic()
                    await runtime.speech_activity(True)
                    cancellation_triggered = True
                    # A stale barrier may finish its shielded tool, but its
                    # lease must not survive the new speech generation.
                    while time.monotonic() - cancel_started < 2.2:
                        if runtime._thinking_lease_turn is None and not runtime._thinking_pending_by_turn:
                            break
                        await asyncio.sleep(0.02)
                    cancellation_latency_ms = round((time.monotonic() - cancel_started) * 1000, 1)
            await wait_runtime(runtime)
            traces = trace_slice(runtime, trace_before)
            meta = metadata_events(trace_events, event_before)
            provider_rows = provider.request_meta[provider_before:]
            dispatches = [row for row in traces if row.get("kind") == "LLM_DISPATCHED"]
            state_rows = [row for row in traces if row.get("kind") == "SPEAK_STATE_CHANGED"]
            thinking_rows = [row for row in traces if row.get("kind") == "THINKING_REQUESTED"]
            barrier_rows = [row for row in traces if row.get("kind") == "THINKING_BARRIER_READY"]
            action_started = [row for row in traces if row.get("kind") == "ACTION_STARTED"]
            action_completed = [row for row in traces if row.get("kind") == "ACTION_COMPLETED"]
            scheduler_tool = [row for row in traces if row.get("kind") == "SCHEDULER_EVENT_ENQUEUED" and row.get("event") == "TOOL_COMPLETED"]
            scheduler_ticks = [row for row in traces if row.get("kind") == "SCHEDULER_TICK" and "TOOL_COMPLETED" in (row.get("events") or [])]
            barrier_at = first_at(traces, "THINKING_BARRIER_READY")
            early = late = 0
            if barrier_at is not None:
                for row in action_completed:
                    if isinstance(row.get("at"), (int, float)):
                        if row["at"] <= barrier_at:
                            early += 1
                        else:
                            late += 1
            errors = sorted({safe_error(RuntimeError(str(row.get("error") or row.get("code") or row.get("reason") or "error")))
                             for row in traces if row.get("kind") in {"AGENT_ERROR", "LLM_PROVIDER_UNAVAILABLE", "LLM_OUTPUT_FILTERED", "SPEAK_ENVELOPE_INVALID", "LLM_EMPTY_OUTPUT", "TOOL_PROTOCOL_ERROR", "RESPONSES_INCOMPLETE"}})
            preview = [row for row in meta if row.get("kind") == "speak.preview"]
            rows.append({
                "scenario": name, "round": round_no, "turn_id_hash": digest(turn_id),
                "input_sha256": digest(prompt), "input_chars": len(prompt),
                "asr_boundary": "finalized_before_tts", "tts_started": False,
                "interrupted": cancellation_triggered, "cancellation_latency_ms": cancellation_latency_ms,
                "requests_dispatched": len(dispatches),
                "provider_requests": [{"finish": item.get("finish"), "error": item.get("error"),
                                        "http_status": item.get("http_status"), "content_chars": item.get("content_chars"),
                                        "reasoning_chars": item.get("reasoning_chars"), "event_count": item.get("events")}
                                       for item in provider_rows],
                "valid_envelopes_inferred": len(state_rows),
                "speak_states": [row.get("state") for row in state_rows if row.get("state")],
                "speak_preview_states": [row.get("state") for row in preview if row.get("state")],
                "speak_prefix_wait_events": sum(1 for row in meta if row.get("kind") == "speech.append_waiting_prefix"),
                "output_replacements": output.replacements, "output_appends": output.appends,
                "output_cancels": output.cancel_count,
                "request_thinking_calls": len(thinking_rows),
                "thinking_barrier_ready": len(barrier_rows),
                "thinking_barrier_timeouts": sum(bool(row.get("timed_out")) for row in barrier_rows),
                "actions_started": len(action_started), "actions_completed": len(action_completed),
                "tool_names": sorted({str(row.get("name")) for row in action_started if row.get("name")}),
                "tool_completed_early": early, "tool_completed_late": late,
                "tool_completion_woke_scheduler": bool(scheduler_tool),
                "tool_completion_scheduler_ticks": len(scheduler_ticks),
                "errors": errors,
                "event_counts": dict(Counter(row.get("kind") for row in traces if row.get("kind"))),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            })
            # Let explicit speech onset end before the next finalized turn.
            if cancellation_triggered:
                await runtime.speech_activity(False)
            await asyncio.sleep(0.02)
    finally:
        runtime.closed = True
        runtime.cancel_active_request("diagnostic_end")
        for task in list(runtime._background_tools):
            task.cancel()
        real_runner.close()
        await client.close()
    return rows


def validate_preflight(raw_parts: list[str], finish: str | None) -> tuple[bool, str | None]:
    raw = "".join(raw_parts)
    if finish != "completed":
        return False, "terminal_not_completed"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return False, "invalid_json"
    if not isinstance(obj, dict) or set(obj) != {"speak", "actions"}:
        return False, "envelope_keys"
    speak = obj.get("speak")
    if not isinstance(speak, dict) or speak.get("state") not in {"speak", "wait", "cancel"}:
        return False, "speak_shape"
    if not isinstance(speak.get("emotion"), str) or not isinstance(speak.get("text"), str):
        return False, "speak_fields"
    if not isinstance(obj.get("actions"), list):
        return False, "actions_shape"
    return True, None


async def preflight(config) -> dict:
    key = config.api_key(config.llm.api_key_env)
    files = FileTools((ROOT / "docs",))
    search_key = config.api_key(config.tts.api_key_env)
    search = WebSearchTools(MiniMaxSearchClient(base_url=config.tts.base_url, api_key=search_key, timeout_ms=5000)) if search_key and config.tools.web_search else None
    runner = ActionRunner(files, search=search)
    definitions = runner.action_definitions()
    schemas = [("no_tools", decision_schema([])), ("tools_and_thinking", decision_schema(definitions, allow_thinking=True))]
    client = DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model, api_key=key,
                            provider="opencode_go", api_key_env=config.llm.api_key_env,
                            # OpenCode Go requires the same product session
                            # header on this preflight as on every soak lane.
                            # Keep the probe on the product client path; a
                            # missing x-opencode-session would misclassify a
                            # valid schema as provider rejection.
                            session_id=f"balanced-v2-preflight-{uuid4().hex}",
                            transport="responses", thinking="disabled", reasoning_effort="low",
                            timeout_ms=max(1000, config.llm.timeout_ms), max_output_tokens=256,
                            protocol_recovery_limit=0, transport_retry_limit=0)
    messages = [ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "只输出严格 JSON 决策对象。"),
                ContextFrame.make(ContextLane.REQUEST_TAIL, "user", "请做最小协议确认。")]
    rows = []
    try:
        for name, schema in schemas:
            started = time.monotonic()
            parts: list[str] = []
            finish = None
            error = None
            status = None
            try:
                async for item in client.stream_events(messages, schema=schema, schema_name="voice_decision_v06", reasoning_effort="none"):
                    if item.get("type") == "content" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif item.get("type") == "finish":
                        finish = item.get("reason")
            except Exception as exc:
                error, status = safe_error(exc), http_status(exc)
            valid, validation_error = validate_preflight(parts, finish) if error is None else (False, error)
            rows.append({"schema": name, "http_status": status or (200 if error is None else None),
                         "terminal": finish, "valid_envelope": valid, "validation_error": validation_error,
                         "content_chars": len("".join(parts)), "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                         "schema_digest": digest(json.dumps(schema, ensure_ascii=False, sort_keys=True))})
    finally:
        runner.close()
        await client.close()
    return {"provider": "opencode_go", "transport": "responses", "credential_present": bool(key),
            "session_header_present": bool(client._request_headers(streaming=True).get("x-opencode-session")),
            "request_thinking_wire_shape": {"name": "request_thinking", "arguments": {"request": True}},
            "rows": rows, "passed": all(row["http_status"] is not None and row["http_status"] < 400 and row["valid_envelope"] for row in rows)}


async def tool_probe(config) -> dict:
    key = config.api_key(config.tts.api_key_env)
    search = WebSearchTools(MiniMaxSearchClient(base_url=config.tts.base_url, api_key=key, timeout_ms=5000)) if key and config.tools.web_search else None
    runner = ActionRunner(FileTools((ROOT / "docs",)), search=search)
    turn = f"tool-probe-{uuid4().hex}"
    rows = []
    try:
        for name, args in (("files_search", {"query": "architecture"}), ("files_read_text", {"path": "architecture.md"}), ("web_search", {"query": "OpenCode Go"})): 
            if name == "web_search" and search is None:
                rows.append({"name": name, "status": "DISABLED", "latency_ms": 0.0})
                continue
            started = time.monotonic()
            try:
                entry, reused = await runner.select(turn, name, args)
                result = await asyncio.shield(entry.task)
                rows.append({"name": name, "status": "completed", "reused": bool(reused),
                             "result_keys": sorted(result.keys()) if isinstance(result, dict) else [type(result).__name__],
                             "latency_ms": round((time.monotonic() - started) * 1000, 1)})
            except Exception as exc:
                rows.append({"name": name, "status": "error", "error": safe_error(exc),
                             "latency_ms": round((time.monotonic() - started) * 1000, 1)})
    finally:
        runner.close()
    return {"real_actionrunner": True, "rows": rows}


async def run(args) -> dict:
    os.environ["VOICE_LLM_PROVIDER"] = "opencode_go"
    config = load_config(ROOT / "config" / "default.yaml", ROOT / "config" / "local.yaml")
    if config.llm.provider != "opencode_go":
        raise RuntimeError(f"provider override failed: {config.llm.provider}")
    if not config.api_key(config.llm.api_key_env):
        raise RuntimeError(f"missing {config.llm.api_key_env}")
    pre = await preflight(config)
    base = {"probe": "balanced-mode-agent-actionrunner-soak-v3", "provider": "opencode_go", "transport": "responses",
            "reasoning_mode": "balanced", "rounds_per_scenario": args.rounds, "source_fingerprint": {
                "decision_schema": file_digest(ROOT / "voice_terminal" / "agent" / "decision_schema.py"),
                "episode_runtime": file_digest(ROOT / "voice_terminal" / "agent" / "episode.py"),
                "action_runner": file_digest(ROOT / "voice_terminal" / "agent" / "speculative_tools.py"),
                "provider_client": file_digest(ROOT / "voice_terminal" / "providers" / "deepseek.py"),
            }, "preflight": pre}
    if not pre["passed"]:
        base.update({"blocked": True, "blocked_reason": "strict_schema_preflight_rejected", "tool_probe": None, "scenarios": [], "rows": []})
        return base
    sem = asyncio.Semaphore(max(1, args.concurrency))
    batches = await asyncio.gather(*[run_lane(name, prompts, config, sem, args.rounds) for name, prompts in SCENARIOS.items()])
    rows = [row for batch in batches for row in batch]
    by_scenario = {}
    for name in SCENARIOS:
        group = [row for row in rows if row["scenario"] == name]
        latencies = [row["elapsed_ms"] for row in group]
        by_scenario[name] = {
            "rounds": len(group), "dispatches": sum(row["requests_dispatched"] for row in group),
            "valid_envelopes_inferred": sum(row["valid_envelopes_inferred"] for row in group),
            "request_thinking_calls": sum(row["request_thinking_calls"] for row in group),
            "thinking_barrier_ready": sum(row["thinking_barrier_ready"] for row in group),
            "thinking_barrier_timeouts": sum(row["thinking_barrier_timeouts"] for row in group),
            "actions_started": sum(row["actions_started"] for row in group),
            "actions_completed": sum(row["actions_completed"] for row in group),
            "tool_completed_early": sum(row["tool_completed_early"] for row in group),
            "tool_completed_late": sum(row["tool_completed_late"] for row in group),
            "tool_completion_scheduler_wakes": sum(row["tool_completion_woke_scheduler"] for row in group),
            "tool_completion_scheduler_ticks": sum(row["tool_completion_scheduler_ticks"] for row in group),
            "interrupted_rounds": sum(row["interrupted"] for row in group),
            "thinking_cancel_attempts": sum(row["request_thinking_calls"] > 0 and row["interrupted"] for row in group),
            "errors": dict(Counter(err for row in group for err in row["errors"])),
            "latency_ms": {"min": min(latencies), "p50": statistics.median(latencies), "max": max(latencies)},
        }
    base.update({"blocked": False, "tool_probe": await tool_probe(config), "scenarios": list(SCENARIOS),
                 "summary": {"scenario_count": len(SCENARIOS), "total_rounds": len(rows),
                              "valid_or_completed_rounds": sum(bool(row["valid_envelopes_inferred"]) and not row["errors"] for row in rows),
                              "rounds_with_errors": sum(bool(row["errors"]) for row in rows),
                              "request_thinking_calls": sum(row["request_thinking_calls"] for row in rows),
                              "thinking_barrier_ready": sum(row["thinking_barrier_ready"] for row in rows),
                              "thinking_barrier_timeouts": sum(row["thinking_barrier_timeouts"] for row in rows),
                              "actions_started": sum(row["actions_started"] for row in rows),
                              "actions_completed": sum(row["actions_completed"] for row in rows),
                              "tool_completion_scheduler_wakes": sum(row["tool_completion_woke_scheduler"] for row in rows),
                              "tool_completion_scheduler_ticks": sum(row["tool_completion_scheduler_ticks"] for row in rows),
                              "interrupted_rounds": sum(row["interrupted"] for row in rows),
                              "errors": dict(Counter(err for row in rows for err in row["errors"]))},
                 "by_scenario": by_scenario, "rows": rows})
    return base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 10:
        raise SystemExit("--rounds must be at least 10")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "blocked": report.get("blocked"), "preflight": report.get("preflight"), "summary": report.get("summary")}, ensure_ascii=False, indent=2))
    return 2 if report.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
