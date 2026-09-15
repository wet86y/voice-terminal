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
from ..core.events import utc_now_iso
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
        on_activity: Callable[[dict[str, Any]], None] | None = None,
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
        self.on_activity = on_activity
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
        # Provider item identifiers never cross this boundary.  They are only
        # used while one activation is being translated into stable local
        # activity items for the product UI.
        self._provider_activity_items: dict[str, str] = {}
        self._provider_activity_item_streams: dict[str, str] = {}

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
        self._activity(
            "lifecycle", "started", item_id=f"run-{serial}",
            text="Shadow Agent 开始运行", run_seq=serial,
        )
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

    def _activity(
        self,
        stream: str,
        phase: str,
        *,
        item_id: str,
        text: str | None = None,
        delta: str | None = None,
        tool_name: str | None = None,
        tool_status: str | None = None,
        event_kind: str | None = None,
        run_seq: int | None = None,
    ) -> None:
        """Publish a bounded, product-safe activity item.

        This relay is intentionally separate from ``ShadowEventBuffer``.  A
        model delta is useful for the live panel, but it is not an
        authoritative Shadow trace entry and must never affect tool ordering
        or context reconciliation.
        """
        if self.on_activity is None:
            return
        payload: dict[str, Any] = {
            "run_seq": self._activation_serial if run_seq is None else run_seq,
            "item_id": str(item_id),
            "stream": str(stream),
            "phase": str(phase),
            "timestamp": utc_now_iso(),
        }
        if text is not None:
            payload["text"] = str(text).strip()[:4000]
        if delta is not None:
            payload["delta"] = str(delta)[:2000]
        if tool_name is not None:
            payload["tool_name"] = str(tool_name)[:160]
        if tool_status is not None:
            payload["tool_status"] = str(tool_status)[:120]
        if event_kind is not None:
            payload["event_kind"] = str(event_kind)
        try:
            self.on_activity(payload)
        except Exception:
            # Activity is diagnostic UI state.  It must never make the
            # authoritative Shadow loop fail because a browser sink closed.
            self._metric("activity.delivery_failed", {"stream": stream, "phase": phase})

    def _activity_id(self, prefix: str, serial: int | None = None) -> str:
        return f"{prefix}-{self._activation_serial if serial is None else serial}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _provider_call(provider: ShadowProvider, kwargs: dict[str, Any]) -> Any:
        """Call current and older test providers without breaking either API."""
        try:
            return provider.complete(**kwargs)
        except TypeError as exc:
            message = str(exc)
            if "on_stream" in message:
                kwargs.pop("on_stream", None)
                try:
                    return provider.complete(**kwargs)
                except TypeError as retry_exc:
                    if "max_output_tokens" not in str(retry_exc):
                        raise
                    kwargs.pop("max_output_tokens", None)
                    return provider.complete(**kwargs)
            if "max_output_tokens" not in message:
                raise
            kwargs.pop("max_output_tokens", None)
            return provider.complete(**kwargs)

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
        retry: bool = True, activity_mode: str | None = None,
        activity_item_id: str | None = None,
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
            attempt_activity_id = (
                activity_item_id if activity_item_id and attempt == 0
                else self._activity_id("model") if activity_mode else ""
            )
            activity_stream = "thinking" if activity_mode == "thinking" else "lifecycle"
            if activity_mode:
                self._activity(
                    activity_stream, "started", item_id=attempt_activity_id,
                    text=("正在整理可读工作简报" if activity_mode == "thinking" else "正在分析 Shadow 上下文"),
                )
            self._provider_activity_items = {}
            self._provider_activity_item_streams = {}

            async def on_stream(event: dict[str, Any]) -> None:
                self._handle_provider_activity(
                    event, mode=activity_mode or "main",
                    activity_item_id=attempt_activity_id,
                )

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
                if activity_mode and self.on_activity is not None:
                    kwargs["on_stream"] = on_stream
                call = self._provider_call(self.provider, kwargs)
                response = await asyncio.wait_for(
                    call, (timeout_ms or self.provider_timeout_ms) / 1000,
                )
                usage = dict(response.usage)
                usage.update({"latency_ms": round((time.monotonic() - started) * 1000, 2)})
                self.usage_samples.append(usage)
                self._metric("provider.completed", usage)
                if activity_mode:
                    self._activity(
                        activity_stream, "completed", item_id=attempt_activity_id,
                        text=("可读工作简报已更新" if activity_mode == "thinking" else "模型处理完成"),
                    )
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if activity_mode:
                    # A provider retry gets fresh local item IDs.  Close any
                    # reasoning/tool item that was left open by the failed
                    # attempt so it cannot remain visually active beside the
                    # retry result.
                    for stale_id in dict.fromkeys(self._provider_activity_items.values()):
                        if stale_id == attempt_activity_id:
                            continue
                        self._activity(
                            self._provider_activity_item_streams.get(stale_id, "thinking"),
                            "failed", item_id=stale_id,
                            text="模型流中断，活动未完成；正在重试" if attempt + 1 < attempts else "模型流中断，活动未完成",
                        )
                    self._activity(
                        activity_stream, "failed", item_id=attempt_activity_id,
                        text=f"本次模型流已结束：{str(exc)[:240] or type(exc).__name__}",
                    )
                self._metric("provider.retry", {"attempt": attempt + 1, "error": type(exc).__name__})
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.15 * (attempt + 1))
        raise ShadowProviderError(str(last)[:500] if last else "provider request failed")

    async def _status_provider_complete(
        self, *, input_items: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]],
        reasoning_effort: str, text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None, timeout_ms: int | None = None,
        retry: bool = False, activity_item_id: str | None = None,
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
        item_id = activity_item_id or self._activity_id("thinking")

        async def on_stream(event: dict[str, Any]) -> None:
            self._handle_provider_activity(event, mode="thinking", activity_item_id=item_id)

        if self.on_activity is not None:
            kwargs["on_stream"] = on_stream
        self._activity(
            "thinking", "started", item_id=item_id, text="正在整理可读工作简报",
        )
        try:
            call = self._provider_call(self.status_provider, kwargs)
            response = await asyncio.wait_for(call, (timeout_ms or self.provider_timeout_ms) / 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._activity(
                "thinking", "failed", item_id=item_id,
                text=f"可读工作简报暂时不可用：{str(exc)[:240] or type(exc).__name__}",
            )
            raise ShadowProviderError(str(exc)[:500]) from exc
        usage = dict(response.usage)
        usage.update({"latency_ms": round((time.monotonic() - started) * 1000, 2)})
        self.status_usage_samples.append(usage)
        self._metric("status_provider.completed", usage)
        self._activity("thinking", "completed", item_id=item_id, text="可读工作简报已更新")
        return response

    def _handle_provider_activity(
        self, event: dict[str, Any], *, mode: str, activity_item_id: str,
    ) -> None:
        """Translate provider lifecycle signals into public-safe activities."""
        kind = event.get("type")
        if not isinstance(kind, str):
            return
        if mode == "thinking":
            # The status observer's output is an intentionally readable
            # summary.  Raw response.reasoning_text.delta is never forwarded.
            if kind == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    self._activity(
                        "thinking", "delta", item_id=activity_item_id, delta=delta,
                    )
            return

        if kind == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            if item_type == "reasoning":
                reasoning_id = self._activity_id("thinking")
                for provider_key in (item.get("id"), item.get("call_id")):
                    if provider_key:
                        self._provider_activity_items[str(provider_key)] = reasoning_id
                self._provider_activity_item_streams[reasoning_id] = "thinking"
                self._activity(
                    "thinking", "started", item_id=reasoning_id,
                    text="正在分析 Shadow 上下文",
                )
            elif item_type == "function_call":
                tool_id = self._activity_id("tool")
                for provider_key in (item.get("id"), item.get("call_id")):
                    if provider_key:
                        self._provider_activity_items[str(provider_key)] = tool_id
                self._provider_activity_item_streams[tool_id] = "tool"
                self._activity(
                    "tool", "started", item_id=tool_id,
                    tool_name=str(item.get("name") or "未知工具"),
                    tool_status="preparing", text="准备调用工具",
                )
            return

        if kind == "response.function_call_arguments.delta":
            provider_key = str(
                event.get("item_id") or event.get("call_id") or event.get("id") or ""
            )
            tool_id = self._provider_activity_items.get(provider_key)
            if tool_id:
                self._activity("tool", "delta", item_id=tool_id, tool_status="preparing")
            return

        if kind == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                return
            local_id = next(
                (self._provider_activity_items.get(str(provider_key))
                 for provider_key in (item.get("id"), item.get("call_id")) if provider_key),
                None,
            )
            if not local_id:
                return
            item_type = item.get("type")
            if item_type == "function_call":
                self._activity(
                    # ``output_item.done`` only means the provider finished
                    # assembling the call.  The actual tool has not run yet;
                    # keep the item live so _execute_calls can transition it
                    # to running and then to its terminal state.
                    "tool", "delta", item_id=local_id,
                    tool_name=str(item.get("name") or "未知工具"),
                    tool_status="ready", text="工具调用已准备",
                )
            elif item_type == "reasoning":
                self._activity(
                    "thinking", "completed", item_id=local_id, text="分析阶段完成",
                )

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
        if kind != "status":
            self._activity(
                "event", "failed" if kind == "failed" else "completed",
                item_id=self._activity_id("event"), text=event.text,
                event_kind=kind,
            )
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
            call_key = str(call.get("call_id") or call.get("id") or "")
            tool_activity_id = self._provider_activity_items.get(call_key)
            if not tool_activity_id:
                tool_activity_id = self._activity_id("tool")
                if call_key:
                    self._provider_activity_items[call_key] = tool_activity_id
            self._provider_activity_item_streams[tool_activity_id] = "tool"
            tool_name = str(call.get("name") or "未知工具")
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
                for item in remaining:
                    remaining_key = str(item.get("call_id") or item.get("id") or "")
                    remaining_id = self._provider_activity_items.get(remaining_key) or self._activity_id("tool")
                    self._activity(
                        "tool", "completed", item_id=remaining_id,
                        tool_name=str(item.get("name") or "未知工具"),
                        tool_status="skipped", text="上下文已更新，未执行",
                    )
                self._metric("tool.prevented_by_context", {"count": len(remaining)})
                break
            call_id, name = call.get("call_id"), call.get("name")
            args = self._arguments(call)
            if not isinstance(name, str) or args is None:
                self._activity(
                    "tool", "failed", item_id=tool_activity_id,
                    tool_name=tool_name, tool_status="failed", text="工具参数无效",
                )
                outputs.append(self._call_output(call_id, {"error": "malformed function call"}))
                continue
            self._activity(
                "tool", "started", item_id=tool_activity_id,
                tool_name=name, tool_status="running", text="正在执行工具",
            )
            if name == "get_latest_context":
                since = args.get("since_revision")
                value = self._latest_context_result(since if isinstance(since, int) else -1)
                outputs.append(self._call_output(call_id, value))
                self._activity(
                    "tool", "completed", item_id=tool_activity_id,
                    tool_name=name, tool_status="completed", text="上下文已同步",
                )
            elif name == "publish_main_event":
                kind, text = args.get("kind"), args.get("text")
                if kind not in {"progress", "observation"} or not isinstance(text, str) or not text.strip():
                    self._activity(
                        "tool", "failed", item_id=tool_activity_id,
                        tool_name=name, tool_status="failed", text="事件内容无效",
                    )
                    outputs.append(self._call_output(call_id, {"error": "invalid event"}))
                else:
                    self._emit(kind, text)
                    outputs.append(self._call_output(call_id, {"status": "published"}))
                    self._activity(
                        "tool", "completed", item_id=tool_activity_id,
                        tool_name=name, tool_status="completed", text="事件已传递",
                    )
            elif name == "request_user_input":
                kind, question = args.get("kind"), args.get("question")
                if kind not in {"clarification", "confirmation"} or not isinstance(question, str) or not question.strip():
                    self._activity(
                        "tool", "failed", item_id=tool_activity_id,
                        tool_name=name, tool_status="failed", text="输入请求无效",
                    )
                    outputs.append(self._call_output(call_id, {"error": "invalid user input request"}))
                else:
                    payload = {
                        "kind": kind,
                        "options": [str(item) for item in args.get("options", []) if isinstance(item, str)],
                        "operation_summary": str(args.get("operation_summary") or ""),
                    }
                    self._emit("user_input_required", question, payload=payload)
                    outputs.append(self._call_output(call_id, {"status": "published", "activation": "idle"}))
                    self._activity(
                        "tool", "completed", item_id=tool_activity_id,
                        tool_name=name, tool_status="waiting", text="等待用户输入",
                    )
                    stop_activation = True
            else:
                tool_started = time.monotonic()
                result = await self.tools.execute(name, args)
                tool_ok = "error" not in result
                self._activity(
                    "tool", "completed" if tool_ok else "failed", item_id=tool_activity_id,
                    tool_name=name, tool_status="completed" if tool_ok else "failed",
                    text="工具执行完成" if tool_ok else str(result.get("error") or "工具执行失败")[:240],
                )
                self._metric("tool.completed", {
                    "name": name,
                    "latency_ms": round((time.monotonic() - tool_started) * 1000, 2),
                    "ok": tool_ok,
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
                activity_mode="main",
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
        activity_item_id = self._activity_id("thinking", serial)

        async def complete(**kwargs: Any) -> ShadowProviderResponse:
            return await self._status_provider_complete(
                activity_item_id=activity_item_id, **kwargs,
            )

        try:
            text = await self.status_observer.summarize(
                self._status_trace_prefix(trace), complete,
            )
            if not text:
                self._activity(
                    "thinking", "completed", item_id=activity_item_id,
                    text="暂无新的可读工作简报",
                )
                return
            newer_visible = self._last_status_trace_revision > trace_revision
            if not self.active or serial != self._activation_serial or newer_visible:
                self._activity(
                    "thinking", "failed", item_id=activity_item_id,
                    text="这次工作简报已过期，未覆盖当前运行",
                )
                self._metric("observer.late_discard", {})
                return
            self._emit("status", text[:512])
            self._activity(
                "thinking", "completed", item_id=activity_item_id,
                text=text[:512],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._activity(
                "thinking", "failed", item_id=activity_item_id,
                text=f"可读工作简报失败：{str(exc)[:240] or type(exc).__name__}",
            )
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
        context_activity_id = f"context-{serial}"
        try:
            self._activity(
                "thinking", "started", item_id=context_activity_id,
                text="正在同步上下文", run_seq=serial,
            )
            self._apply_pending_context()
            self._activity(
                "thinking", "completed", item_id=context_activity_id,
                text="上下文已同步", run_seq=serial,
            )
            while not self._closed and serial == self._activation_serial:
                self._apply_pending_context()
                await self._maybe_compact()
                response = await self._provider_complete(
                    input_items=tuple(self.working_trace), tools=self._tools,
                    reasoning_effort=self.reasoning_effort,
                    activity_mode="main",
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
                            call_key = str(call.get("call_id") or call.get("id") or "")
                            tool_activity_id = self._provider_activity_items.get(call_key) or self._activity_id("tool")
                            self._provider_activity_item_streams[tool_activity_id] = "tool"
                            self._activity(
                                "tool", "completed", item_id=tool_activity_id,
                                tool_name=str(call.get("name") or "未知工具"),
                                tool_status="skipped", text="上下文已更新，未执行",
                            )
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
                        self._activity(
                            "lifecycle", "completed", item_id=f"run-{serial}",
                            text="等待用户输入", run_seq=serial,
                        )
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
                    self._activity(
                        "lifecycle", "completed", item_id=f"run-{serial}",
                        text="Shadow Agent 已完成本轮处理", run_seq=serial,
                    )
                elif state == "no_action":
                    self._activity(
                        "lifecycle", "completed", item_id=f"run-{serial}",
                        text="本轮无需继续操作", run_seq=serial,
                    )
                self._last_processed_revision = self.applied_context_revision
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed and serial == self._activation_serial:
                self._emit("failed", f"影子 Agent 暂时无法继续：{str(exc)[:300]}", payload={"category": type(exc).__name__})
                self._activity(
                    "lifecycle", "failed", item_id=f"run-{serial}",
                    text=f"Shadow Agent 运行失败：{str(exc)[:240] or type(exc).__name__}",
                    run_seq=serial,
                )
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
        self._provider_activity_items = {}
        self._provider_activity_item_streams = {}
        self.usage_samples.clear()
        self.status_usage_samples.clear()

    async def close(self, *, close_tools: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        await self.reset()
        if close_tools:
            self.tools.close()
