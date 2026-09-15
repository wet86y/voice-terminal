"""Session-scoped, single-instance Shadow Agent runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .contracts import (
    ShadowContextFrame,
    ShadowContextSnapshot,
    ShadowContextUpdate,
    ShadowEvent,
    ShadowVisibleStatus,
    WakeResult,
    shadow_control_tool_definitions,
)
from .prompts import COMPACTION_PROMPT, INTENT_REASSESSMENT_PROMPT, STATUS_OBSERVER_PROMPT
from .provider import ShadowProvider, ShadowProviderError, ShadowProviderResponse
from .tools import ShadowToolExecutor


log = logging.getLogger(__name__)


class ShadowContextSource(Protocol):
    @property
    def shadow_input_revision(self) -> int: ...

    @property
    def shadow_context_epoch(self) -> int: ...

    def shadow_context_snapshot(self) -> ShadowContextSnapshot: ...

    def shadow_context_update(self, since_revision: int, *, epoch: int) -> ShadowContextUpdate | ShadowContextSnapshot: ...


def _frame_payload(frame: ShadowContextFrame) -> dict[str, Any]:
    return {
        "revision": frame.revision, "origin": frame.origin,
        "kind": frame.kind, "content": frame.content,
    }


def _text_from_output(output: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                    value = block.get("text")
                    if isinstance(value, str):
                        parts.append(value)
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


class ShadowStatusObserver:
    """Stateless, read-only summarizer over an immutable committed prefix."""

    def __init__(self, *, max_output_tokens: int = 512, timeout_ms: int = 10000) -> None:
        self.max_output_tokens = max_output_tokens
        self.timeout_ms = timeout_ms

    async def summarize(self, trace: Sequence[dict[str, Any]], complete: Callable[..., Any]) -> str:
        response = await complete(
            input_items=[*trace, {"role": "system", "content": STATUS_OBSERVER_PROMPT}],
            tools=[], reasoning_effort="none", max_output_tokens=self.max_output_tokens,
            timeout_ms=self.timeout_ms, retry=False,
        )
        return _text_from_output(response.output).strip()


class ShadowRuntime:
    """One durable cognitive/tool loop for one conversation session.

    Wakes carry no task semantics.  They only advance the newest authoritative
    context cursor; all intent recognition happens inside this loop.
    """

    def __init__(
        self,
        provider: ShadowProvider,
        tools: ShadowToolExecutor,
        context_source: ShadowContextSource,
        *,
        instructions_loader: Callable[[], str],
        on_event: Callable[[ShadowEvent], None] | None = None,
        provider_timeout_ms: int = 15000,
        transport_retry_limit: int = 2,
        max_context_tokens: int = 524288,
        compaction_threshold_ratio: float = 0.80,
        observer_enabled: bool = True,
        observer_every_steps: int = 5,
        observer_max_output_tokens: int = 512,
        observer_timeout_ms: int = 10000,
        status_provider: ShadowProvider | None = None,
        observer_max_context_tokens: int = 32768,
        reasoning_effort: str = "high",
        allowed_reasoning: Sequence[str] | None = None,
        on_metric: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if transport_retry_limit < 0 or observer_every_steps < 1 or reasoning_effort not in {"off", "low", "medium", "high", "max"}:
            raise ValueError("invalid Shadow runtime retry/observer configuration")
        if max_context_tokens < 1024 or observer_max_context_tokens < 1024 or not 0 < compaction_threshold_ratio < 1:
            raise ValueError("invalid Shadow context budget")
        tools.assert_read_only()
        self.provider = provider
        self.status_provider = status_provider or provider
        self.tools = tools
        self.context_source = context_source
        self.instructions_loader = instructions_loader
        self.on_event = on_event
        self.on_metric = on_metric
        self.provider_timeout_ms = provider_timeout_ms
        self.transport_retry_limit = transport_retry_limit
        self.max_context_tokens = max_context_tokens
        self.compaction_threshold_ratio = compaction_threshold_ratio
        self.observer_enabled = observer_enabled
        self.observer_every_steps = observer_every_steps
        self.observer_max_output_tokens = observer_max_output_tokens
        self.observer_timeout_ms = observer_timeout_ms
        self.observer_max_context_tokens = observer_max_context_tokens
        self.reasoning_effort = reasoning_effort
        raw_allowed = ("off", "low", "medium", "high", "max") if allowed_reasoning is None else allowed_reasoning
        self.allowed_reasoning = tuple(dict.fromkeys(str(value).strip().lower() for value in raw_allowed))
        if not self.allowed_reasoning or any(
            value not in {"off", "low", "medium", "high", "max"}
            for value in self.allowed_reasoning
        ) or self.reasoning_effort not in self.allowed_reasoning:
            raise ValueError("invalid Shadow reasoning policy")
        self.status_observer = ShadowStatusObserver(
            max_output_tokens=observer_max_output_tokens,
            timeout_ms=observer_timeout_ms,
        )

        self.active = False
        self.context_epoch = 0
        self.applied_context_revision = 0
        self.pending_context_revision = 0
        self.trace_revision = 0
        self.committed_model_steps = 0
        self.last_status_step = 0
        self.working_trace: list[dict[str, Any]] = []
        self.latest_visible_status: ShadowVisibleStatus | None = None
        self.user_input_required = False
        self.runner: asyncio.Task[None] | None = None
        self.observer_task: asyncio.Task[None] | None = None

        self._source_epoch = 0
        self._last_processed_revision = -1
        self._activation_serial = 0
        self._observer_pending = False
        self._last_status_trace_revision = 0
        self._compaction_retry_pending = False
        self._pending_since: float | None = None
        self._closed = False
        self._instructions = instructions_loader()
        self._tools = tuple([*tools.responses_tools(), *shadow_control_tool_definitions()])
        self.usage_samples: deque[dict[str, Any]] = deque(maxlen=256)
        self.status_usage_samples: deque[dict[str, Any]] = deque(maxlen=64)

    @property
    def available(self) -> bool:
        return not self._closed

    def reload_instructions(self) -> None:
        """Reload the allowlisted Shadow persona at an idle session boundary."""
        self._instructions = self.instructions_loader()

    def wake(self) -> WakeResult:
        if self._closed:
            return "unavailable"
        revision = self.context_source.shadow_input_revision
        if revision > self.applied_context_revision and self._pending_since is None:
            self._pending_since = time.monotonic()
        self.pending_context_revision = max(self.pending_context_revision, revision)
        if self.active:
            return "coalesced"
        if revision <= self._last_processed_revision:
            return "already_current"
        self.active = True
        self.user_input_required = False
        self._notify_runtime_state(active=True)
        self._activation_serial += 1
        serial = self._activation_serial
        self.runner = asyncio.create_task(self._run_activation(serial), name="shadow-agent-runtime")
        self._metric("activation.started", {"revision": revision, "serial": serial})
        return "started"

    def _notify_runtime_state(self, *, active: bool) -> None:
        setter = getattr(self.context_source, "set_shadow_runtime_state", None)
        if callable(setter):
            try:
                setter(available=not self._closed, active=active)
            except Exception:
                pass

    def _metric(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_metric is not None:
            try:
                self.on_metric(name, dict(payload))
            except Exception:
                pass

    def _append(self, item: dict[str, Any]) -> None:
        self.working_trace.append(item)
        self.trace_revision += 1

    def _append_context(self, label: str, payload: dict[str, Any]) -> None:
        self._append({
            "role": "system",
            "content": label + "\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })
        self._append({"role": "system", "content": INTENT_REASSESSMENT_PROMPT})

    def _apply_pending_context(self) -> bool:
        latest = self.context_source.shadow_input_revision
        self.pending_context_revision = max(self.pending_context_revision, latest)
        source_epoch = self.context_source.shadow_context_epoch
        if not self.working_trace or source_epoch != self._source_epoch:
            snapshot = self.context_source.shadow_context_snapshot()
            self._source_epoch = snapshot.epoch
            self.context_epoch = max(1, self.context_epoch + 1)
            self.applied_context_revision = snapshot.revision
            self.pending_context_revision = max(self.pending_context_revision, snapshot.revision)
            self._append_context("【权威会话完整快照】", {
                "epoch": snapshot.epoch,
                "frames": [_frame_payload(frame) for frame in snapshot.frames],
            })
            self._metric("context.applied", {
                "revision": self.applied_context_revision,
                "pending_revision": self.pending_context_revision,
                "latency_ms": (
                    round((time.monotonic() - self._pending_since) * 1000, 2)
                    if self._pending_since is not None else 0.0
                ),
            })
            self._pending_since = None
            return True
        if self.pending_context_revision <= self.applied_context_revision:
            return False
        update = self.context_source.shadow_context_update(
            self.applied_context_revision, epoch=self._source_epoch,
        )
        if isinstance(update, ShadowContextSnapshot):
            self._source_epoch = update.epoch
            self.applied_context_revision = update.revision
            self._append_context("【权威会话重新同步快照】", {
                "epoch": update.epoch,
                "frames": [_frame_payload(frame) for frame in update.frames],
            })
        else:
            self.applied_context_revision = update.to_revision
            self._append_context("【权威会话上下文更新】", {
                "frames": [_frame_payload(frame) for frame in update.frames],
            })
        self._metric("context.applied", {
            "revision": self.applied_context_revision,
            "pending_revision": self.pending_context_revision,
            "latency_ms": (
                round((time.monotonic() - self._pending_since) * 1000, 2)
                if self._pending_since is not None else 0.0
            ),
        })
        self._pending_since = None
        return True

    async def _provider_complete(
        self, *, input_items: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]],
        reasoning_effort: str, text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None, timeout_ms: int | None = None,
        retry: bool = True,
    ) -> ShadowProviderResponse:
        normalized_effort = "off" if reasoning_effort == "none" else reasoning_effort
        if normalized_effort not in self.allowed_reasoning:
            raise ShadowProviderError(
                f"Shadow 模型不支持思考等级 {normalized_effort}；允许值为 "
                f"{', '.join(self.allowed_reasoning)}。"
            )
        attempts = self.transport_retry_limit + 1 if retry else 1
        last: BaseException | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                kwargs: dict[str, Any] = {
                    "instructions": self._instructions,
                    "input_items": input_items,
                    "tools": tools,
                    "reasoning_effort": normalized_effort,
                    "text_schema": text_schema,
                }
                if max_output_tokens is not None:
                    kwargs["max_output_tokens"] = max_output_tokens
                try:
                    call = self.provider.complete(**kwargs)
                except TypeError as exc:
                    if "max_output_tokens" not in str(exc):
                        raise
                    kwargs.pop("max_output_tokens", None)
                    call = self.provider.complete(**kwargs)
                response = await asyncio.wait_for(
                    call, (timeout_ms or self.provider_timeout_ms) / 1000,
                )
                usage = dict(response.usage)
                usage.update({"latency_ms": round((time.monotonic() - started) * 1000, 2)})
                self.usage_samples.append(usage)
                self._metric("provider.completed", usage)
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                self._metric("provider.retry", {"attempt": attempt + 1, "error": type(exc).__name__})
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.15 * (attempt + 1))
        raise ShadowProviderError(str(last)[:500] if last else "provider request failed")

    async def _status_provider_complete(
        self, *, input_items: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]],
        reasoning_effort: str, text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None, timeout_ms: int | None = None,
        retry: bool = False,
    ) -> ShadowProviderResponse:
        """Status reporting is isolated: no tools and always no reasoning."""
        if tools or reasoning_effort != "none":
            raise ShadowProviderError("status observer must not use tools or reasoning")
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "instructions": self._instructions, "input_items": input_items,
            "tools": [], "reasoning_effort": "none", "text_schema": text_schema,
        }
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        try:
            try:
                call = self.status_provider.complete(**kwargs)
            except TypeError as exc:
                if "max_output_tokens" not in str(exc):
                    raise
                kwargs.pop("max_output_tokens", None)
                call = self.status_provider.complete(**kwargs)
            response = await asyncio.wait_for(call, (timeout_ms or self.provider_timeout_ms) / 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ShadowProviderError(str(exc)[:500]) from exc
        usage = dict(response.usage)
        usage.update({"latency_ms": round((time.monotonic() - started) * 1000, 2)})
        self.status_usage_samples.append(usage)
        self._metric("status_provider.completed", usage)
        return response

    @staticmethod
    def _calls(output: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(item) for item in output if item.get("type") == "function_call"]

    @staticmethod
    def _arguments(call: dict[str, Any]) -> dict[str, Any] | None:
        raw = call.get("arguments")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _call_output(call_id: Any, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function_call_output", "call_id": str(call_id or "invalid"),
            "output": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        }

    def _emit(self, kind: str, text: str, *, payload: dict[str, Any] | None = None) -> None:
        wake_policy = {
            "progress": "passive", "status": "normal", "observation": "normal",
            "user_input_required": "priority", "result": "priority", "failed": "normal",
        }[kind]
        event = ShadowEvent(
            event_id=uuid.uuid4().hex,
            kind=kind,  # type: ignore[arg-type]
            text=str(text).strip()[:8000],
            based_on_context_revision=self.applied_context_revision,
            wake_policy=wake_policy,  # type: ignore[arg-type]
            payload=dict(payload or {}),
        )
        if kind in {"status", "progress"}:
            self.latest_visible_status = ShadowVisibleStatus(
                active=self.active, text=event.text,
                based_on_trace_revision=self.trace_revision, updated_at=time.time(),
            )
            if kind == "status":
                self._last_status_trace_revision = self.trace_revision
        if kind == "user_input_required":
            self.user_input_required = True
        elif kind in {"result", "failed"}:
            self.user_input_required = False
        if self.on_event is not None:
            self.on_event(event)
        self._metric("event.published", {"kind": kind})

    def _latest_context_result(self, since_revision: int) -> dict[str, Any]:
        latest = self.context_source.shadow_input_revision
        if since_revision == latest and self.context_source.shadow_context_epoch == self._source_epoch:
            return {"status": "unchanged", "epoch": self._source_epoch, "revision": latest, "frames": []}
        update = self.context_source.shadow_context_update(since_revision, epoch=self._source_epoch)
        if isinstance(update, ShadowContextSnapshot):
            return {
                "status": "snapshot", "epoch": update.epoch, "revision": update.revision,
                "frames": [_frame_payload(frame) for frame in update.frames],
            }
        return {
            "status": "delta", "epoch": update.epoch, "from_revision": update.from_revision,
            "revision": update.to_revision,
            "frames": [_frame_payload(frame) for frame in update.frames],
        }

    async def _execute_calls(self, calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        outputs: list[dict[str, Any]] = []
        stop_activation = False
        for index, call in enumerate(calls):
            if index and self.context_source.shadow_input_revision > self.applied_context_revision:
                remaining = calls[index:]
                outputs.extend(
                    self._call_output(item.get("call_id"), {
                        "status": "not_executed", "reason": "conversation_context_updated",
                    })
                    for item in remaining
                )
                self.pending_context_revision = max(
                    self.pending_context_revision, self.context_source.shadow_input_revision,
                )
                self._metric("tool.prevented_by_context", {"count": len(remaining)})
                break
            call_id, name = call.get("call_id"), call.get("name")
            args = self._arguments(call)
            if not isinstance(name, str) or args is None:
                outputs.append(self._call_output(call_id, {"error": "malformed function call"}))
                continue
            if name == "get_latest_context":
                since = args.get("since_revision")
                value = self._latest_context_result(since if isinstance(since, int) else -1)
                outputs.append(self._call_output(call_id, value))
            elif name == "publish_main_event":
                kind, text = args.get("kind"), args.get("text")
                if kind not in {"progress", "observation"} or not isinstance(text, str) or not text.strip():
                    outputs.append(self._call_output(call_id, {"error": "invalid event"}))
                else:
                    self._emit(kind, text)
                    outputs.append(self._call_output(call_id, {"status": "published"}))
            elif name == "request_user_input":
                kind, question = args.get("kind"), args.get("question")
                if kind not in {"clarification", "confirmation"} or not isinstance(question, str) or not question.strip():
                    outputs.append(self._call_output(call_id, {"error": "invalid user input request"}))
                else:
                    payload = {
                        "kind": kind,
                        "options": [str(item) for item in args.get("options", []) if isinstance(item, str)],
                        "operation_summary": str(args.get("operation_summary") or ""),
                    }
                    self._emit("user_input_required", question, payload=payload)
                    outputs.append(self._call_output(call_id, {"status": "published", "activation": "idle"}))
                    stop_activation = True
            else:
                tool_started = time.monotonic()
                result = await self.tools.execute(name, args)
                self._metric("tool.completed", {
                    "name": name,
                    "latency_ms": round((time.monotonic() - tool_started) * 1000, 2),
                    "ok": "error" not in result,
                })
                outputs.append(self._call_output(call_id, result))
            if stop_activation:
                break
        return outputs, stop_activation

    def _estimate_tokens(self) -> int:
        raw = json.dumps(self.working_trace, ensure_ascii=False, separators=(",", ":"))
        ascii_count = sum(ord(char) < 128 for char in raw)
        return max(1, (ascii_count + 3) // 4 + len(raw) - ascii_count)

    @staticmethod
    def _estimate_items_tokens(items: Sequence[dict[str, Any]]) -> int:
        raw = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        ascii_count = sum(ord(char) < 128 for char in raw)
        return max(1, (ascii_count + 3) // 4 + len(raw) - ascii_count)

    def _status_trace_prefix(self, trace: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
        """Keep only a bounded, committed tail for the independent observer."""
        selected: list[dict[str, Any]] = []
        for item in reversed(trace):
            candidate = [dict(item), *selected]
            if self._estimate_items_tokens(candidate) > self.observer_max_context_tokens:
                break
            selected = candidate
        return tuple(selected)

    async def _maybe_compact(self) -> None:
        before = self._estimate_tokens()
        if before < int(self.max_context_tokens * self.compaction_threshold_ratio):
            return
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["history_summary", "working_summary", "pending_state"],
            "properties": {
                "history_summary": {"type": "string"}, "working_summary": {"type": "string"},
                "pending_state": {"type": "string"},
            },
        }
        try:
            response = await self._provider_complete(
                input_items=[*self.working_trace, {"role": "system", "content": COMPACTION_PROMPT}],
                tools=[], reasoning_effort=self.reasoning_effort, text_schema=schema, retry=False,
            )
            text = _text_from_output(response.output)
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("invalid compaction object")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._compaction_retry_pending = True
            self._metric("compaction.failed", {"error": type(exc).__name__, "tokens": before})
            return
        compact_snapshot = getattr(self.context_source, "shadow_compaction_snapshot", None)
        snapshot = (
            compact_snapshot(recent_interactions=10)
            if callable(compact_snapshot)
            else self.context_source.shadow_context_snapshot()
        )
        self.context_epoch += 1
        self.working_trace = []
        self.trace_revision += 1
        self._append({"role": "system", "content": "【稳定历史与工作摘要】\n" + json.dumps(value, ensure_ascii=False)})
        self._append_context("【当前权威会话快照】", {
            "epoch": snapshot.epoch,
            "frames": [_frame_payload(frame) for frame in snapshot.frames],
        })
        self._source_epoch = snapshot.epoch
        self.applied_context_revision = snapshot.revision
        self._compaction_retry_pending = False
        self._metric("compaction.completed", {
            "epoch": self.context_epoch, "before_tokens": before, "after_tokens": self._estimate_tokens(),
        })

    def _schedule_observer(self, serial: int) -> None:
        if not self.observer_enabled or self.committed_model_steps - self.last_status_step < self.observer_every_steps:
            return
        if self.observer_task is not None and not self.observer_task.done():
            self._observer_pending = True
            return
        self.last_status_step = self.committed_model_steps
        trace = tuple(dict(item) for item in self.working_trace)
        trace_revision = self.trace_revision
        self.observer_task = asyncio.create_task(
            self._observe(serial, trace, trace_revision), name="shadow-status-observer",
        )

    async def _observe(self, serial: int, trace: tuple[dict[str, Any], ...], trace_revision: int) -> None:
        try:
            text = await self.status_observer.summarize(
                self._status_trace_prefix(trace), self._status_provider_complete,
            )
            if not text:
                return
            newer_visible = self._last_status_trace_revision > trace_revision
            if not self.active or serial != self._activation_serial or newer_visible:
                self._metric("observer.late_discard", {})
                return
            self._emit("status", text[:512])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._metric("observer.failed", {"error": type(exc).__name__})
        finally:
            pending = self._observer_pending
            self._observer_pending = False
            if pending and self.active and serial == self._activation_serial:
                asyncio.get_running_loop().call_soon(self._schedule_observer, serial)

    def _terminal_text(self, text: str) -> tuple[str, str]:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return ("result", text)
        if isinstance(value, dict) and value.get("state") == "no_action":
            return ("no_action", str(value.get("text") or ""))
        if isinstance(value, dict) and value.get("state") == "result":
            return ("result", str(value.get("text") or value.get("result") or ""))
        return ("result", text)

    async def _run_activation(self, serial: int) -> None:
        started = time.monotonic()
        try:
            while not self._closed and serial == self._activation_serial:
                self._apply_pending_context()
                await self._maybe_compact()
                response = await self._provider_complete(
                    input_items=tuple(self.working_trace), tools=self._tools,
                    reasoning_effort=self.reasoning_effort,
                )
                for item in response.output:
                    self._append(dict(item))
                self.committed_model_steps += 1
                calls = self._calls(response.output)
                if calls:
                    # Guidance wins at the safe boundary before any old call starts.
                    latest = self.context_source.shadow_input_revision
                    if latest > self.applied_context_revision or self.pending_context_revision > self.applied_context_revision:
                        self.pending_context_revision = max(self.pending_context_revision, latest)
                        for call in calls:
                            self._append(self._call_output(call.get("call_id"), {
                                "status": "not_executed", "reason": "conversation_context_updated",
                            }))
                        self._metric("tool.prevented_by_context", {"count": len(calls)})
                        self._schedule_observer(serial)
                        continue
                    outputs, stop = await self._execute_calls(calls)
                    for output in outputs:
                        self._append(output)
                    # Context arriving during a read-only call is applied before the next provider step.
                    self.pending_context_revision = max(
                        self.pending_context_revision, self.context_source.shadow_input_revision,
                    )
                    if stop:
                        self._last_processed_revision = self.applied_context_revision
                        return
                    self._schedule_observer(serial)
                    continue
                text = _text_from_output(response.output)
                if not text:
                    raise ShadowProviderError("completed response contained neither tool calls nor terminal text")
                # Never publish a final drafted against a stale authority revision.
                latest = self.context_source.shadow_input_revision
                if latest > self.applied_context_revision or self.pending_context_revision > self.applied_context_revision:
                    self.pending_context_revision = max(self.pending_context_revision, latest)
                    self._metric("result.reconciled", {"draft_revision": self.applied_context_revision})
                    continue
                state, terminal = self._terminal_text(text)
                if state == "result" and terminal.strip():
                    self._emit("result", terminal)
                self._last_processed_revision = self.applied_context_revision
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed and serial == self._activation_serial:
                self._emit("failed", f"影子 Agent 暂时无法继续：{str(exc)[:300]}", payload={"category": type(exc).__name__})
                self._last_processed_revision = self.applied_context_revision
        finally:
            if serial == self._activation_serial:
                self.active = False
                self._notify_runtime_state(active=False)
                if self.latest_visible_status is not None:
                    self.latest_visible_status = ShadowVisibleStatus(
                        active=False, text=self.latest_visible_status.text,
                        based_on_trace_revision=self.latest_visible_status.based_on_trace_revision,
                        updated_at=self.latest_visible_status.updated_at,
                    )
                self._metric("activation.finished", {
                    "serial": serial, "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "committed_steps": self.committed_model_steps,
                })

    def snapshot(self) -> dict[str, Any]:
        context_tokens = self._estimate_tokens()
        usage = self.usage_samples[-1] if self.usage_samples else None
        input_tokens = usage.get("input_tokens") if usage else None
        cached_tokens = usage.get("cached_tokens") if usage else None
        cache_ratio = usage.get("cache_hit_ratio") if usage else None
        return {
            "available": self.available, "active": self.active,
            "status": self.latest_visible_status.text if self.latest_visible_status else "",
            "user_input_required": self.user_input_required,
            "context_epoch": self.context_epoch,
            "applied_context_revision": self.applied_context_revision,
            "pending_context_revision": self.pending_context_revision,
            "committed_model_steps": self.committed_model_steps,
            "context_token_estimate": context_tokens,
            "context_limit_tokens": self.max_context_tokens,
            "context_usage_percent": round(context_tokens / self.max_context_tokens * 100, 2),
            "provider_input_tokens": input_tokens,
            "provider_cached_tokens": cached_tokens,
            "provider_cache_hit_ratio": cache_ratio,
        }

    async def reset(self) -> None:
        self._activation_serial += 1
        for task in (self.runner, self.observer_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.runner, self.observer_task) if task is not None),
            return_exceptions=True,
        )
        self.active = False
        self._notify_runtime_state(active=False)
        self.context_epoch += 1
        self.applied_context_revision = 0
        self.pending_context_revision = 0
        self.trace_revision = 0
        self.committed_model_steps = 0
        self.last_status_step = 0
        self.working_trace.clear()
        self.latest_visible_status = None
        self.user_input_required = False
        self.runner = None
        self.observer_task = None
        self._source_epoch = 0
        self._last_processed_revision = -1
        self._observer_pending = False
        self._last_status_trace_revision = 0
        self._pending_since = None
        self.usage_samples.clear()
        self.status_usage_samples.clear()

    async def close(self, *, close_tools: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        await self.reset()
        if close_tools:
            self.tools.close()
