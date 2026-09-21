"""Authenticated Voice Terminal side of the first-party DSH Shadow Bridge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from ..core.events import utc_now_iso
from ..shadow_agent.contracts import (
    ShadowContextFrame,
    ShadowContextSnapshot,
    ShadowContextUpdate,
    ShadowEvent,
    WakeResult,
)
from ..shadow_agent.manager import ShadowContextSource
from ..shadow_agent.manager import ShadowStatusObserver
from ..shadow_agent.provider import ShadowProvider, ShadowProviderError, ShadowProviderResponse


BRIDGE_PROTOCOL_VERSION = "1.0"
log = logging.getLogger(__name__)

_STATUS_OBSERVER_INSTRUCTIONS = (
    "You are the isolated, read-only Voice Terminal Shadow status observer. "
    "Summarize only the committed ObserverSnapshot supplied in the request. "
    "Never use tools, continue the task, or expose private reasoning."
)
BridgeResult = Literal[
    "accepted", "duplicate", "stale", "resync_required", "conflict",
    "not_found", "unauthorized",
]
EventSink = Callable[[list[ShadowEvent]], Awaitable[None]]
ActivitySink = Callable[[list[dict[str, Any]]], Awaitable[None]]
AvailabilityWaiter = Callable[[], Awaitable[bool]]


class DshBridgeError(RuntimeError):
    """A protocol or transport failure without sensitive response material."""


def new_bridge_token() -> str:
    return secrets.token_urlsafe(48)


def _frame(frame: ShadowContextFrame) -> dict[str, Any]:
    return {
        "revision": frame.revision,
        "origin": frame.origin,
        "kind": frame.kind,
        "content": frame.content,
    }


def _safe_identifier(value: str, *, field: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 160 or any(char in value for char in "\r\n\0"):
        raise DshBridgeError(f"invalid {field}")
    return value


@dataclass
class DshSessionMapping:
    schema_version: int
    voice_session_id: str
    backend: str
    dsh_session_id: str
    epoch: int
    state: str
    last_applied_revision: int
    last_activation_id: str | None
    last_accepted_event_id: str | None
    created_at: float
    updated_at: float
    retired_at: float | None = None


class DshMappingStore:
    """Project-private, non-sensitive, atomic Voice↔DSH binding store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self, voice_session_id: str) -> DshSessionMapping | None:
        async with self._lock:
            rows = await asyncio.to_thread(self._read)
            row = rows.get(voice_session_id)
            return DshSessionMapping(**row) if isinstance(row, dict) else None

    async def save(self, mapping: DshSessionMapping) -> None:
        async with self._lock:
            rows = await asyncio.to_thread(self._read)
            rows[mapping.voice_session_id] = asdict(mapping)
            await asyncio.to_thread(self._write, rows)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DshBridgeError("DSH mapping state is unreadable") from None
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise DshBridgeError("DSH mapping schema is unsupported")
        sessions = value.get("sessions")
        if not isinstance(sessions, dict):
            raise DshBridgeError("DSH mapping state is malformed")
        return sessions

    def _write(self, sessions: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            {"schema_version": 1, "sessions": sessions},
            ensure_ascii=False, sort_keys=True, indent=2,
        )
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)


class DshBridgeClient:
    """Small strict client; every DSH operation stays behind the plugin route."""

    def __init__(self, *, origin: str, token: str, timeout_ms: int = 15000) -> None:
        if origin != "http://127.0.0.1:3081":
            raise ValueError("DSH Bridge origin must be http://127.0.0.1:3081")
        if len(token) < 32:
            raise ValueError("DSH Bridge token is too short")
        self.origin = origin
        self._token = token
        self.timeout_s = timeout_ms / 1000

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "health", None)

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "request_id": str(payload.get("request_id") or uuid.uuid4()),
            **payload,
        }
        response = await self._request("POST", operation, body)
        if response.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
            raise DshBridgeError("DSH Bridge protocol mismatch")
        return response

    async def _request(
        self, method: str, operation: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_sync, method, operation, payload)

    def _request_sync(
        self, method: str, operation: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}/api/voice-terminal-shadow/v1/{operation}",
            data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DshBridgeError(f"DSH Bridge transport failed: {type(exc).__name__}") from None
        if len(raw) > 2 * 1024 * 1024:
            raise DshBridgeError("DSH Bridge response exceeds limit")
        try:
            value = json.loads(raw)
        except ValueError:
            raise DshBridgeError("DSH Bridge returned invalid JSON") from None
        if not isinstance(value, dict):
            raise DshBridgeError("DSH Bridge returned a non-object response")
        return value


class DshShadowAgentSession:
    """Voice-side session facade for one formal DSH Shadow root Agent."""

    def __init__(
        self,
        *,
        client: DshBridgeClient,
        mapping_store: DshMappingStore,
        context_source: ShadowContextSource,
        voice_session_id: str,
        workspace: Path,
        production_sink: EventSink | None = None,
        activity_sink: ActivitySink | None = None,
        status_provider: ShadowProvider | None = None,
        observer_every_steps: int = 5,
        observer_max_output_tokens: int = 512,
        observer_timeout_ms: int | None = None,
        availability_waiter: AvailabilityWaiter | None = None,
    ) -> None:
        self.client = client
        self.mapping_store = mapping_store
        self.context_source = context_source
        self.voice_session_id = _safe_identifier(voice_session_id, field="voice_session_id")
        self.workspace = workspace.resolve()
        self.production_sink = production_sink
        self.activity_sink = activity_sink
        self.status_provider = status_provider
        self.availability_waiter = availability_waiter
        self.observer_every_steps = max(1, int(observer_every_steps))
        self._observer = ShadowStatusObserver(
            max_output_tokens=observer_max_output_tokens,
            timeout_ms=observer_timeout_ms,
        )
        self.dsh_session_id = f"voice-shadow-{uuid.uuid4()}"
        self.epoch = int(context_source.shadow_context_epoch)
        self.last_applied_revision = -1
        self.activation_id: str | None = None
        self._opened = False
        self._retired = False
        self._wake_lock = asyncio.Lock()
        self._accepted_events: dict[str, str] = {}
        self._waiting_user = False
        self._created_at = time.time()
        self._mapping_checked = False
        self._observer_trace_revision = 0
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_target_revision = 0
        self._observer_force = False
        self._visible_status = ""
        self._run_seq = 0
        self._observer_committed_steps = 0

    @property
    def available(self) -> bool:
        return not self._retired

    def set_context_gate_enabled(self, _enabled: bool) -> bool:
        """DSH deliberately ignores the legacy native-only context gate."""
        return False

    def replace_context_source(self, source: ShadowContextSource) -> None:
        self.context_source = source
        if not self._opened:
            self.epoch = int(source.shadow_context_epoch)

    def rotate_voice_session_id(self, voice_session_id: str) -> None:
        if self._opened:
            raise DshBridgeError("cannot rotate Voice Session while DSH binding is open")
        self.voice_session_id = _safe_identifier(voice_session_id, field="voice_session_id")
        self._created_at = time.time()
        setter = getattr(self.status_provider, "set_session_id", None)
        if callable(setter):
            setter(self.voice_session_id)

    def reload_instructions(self) -> None:
        # The generated DSH preset is synchronized by setup and validated at boot.
        return None

    def wake(self) -> WakeResult:
        if not self.available:
            return "unavailable"
        asyncio.create_task(self.request_wake(), name="dsh-shadow-wake")
        return "started"

    async def request_wake(self) -> WakeResult:
        if not self.available:
            return "unavailable"
        async with self._wake_lock:
            if self.availability_waiter is not None and not await self.availability_waiter():
                return "unavailable"
            await self._ensure_open()
            await self._sync_context()
            if self.activation_id is not None:
                return "coalesced"
            revision = self.last_applied_revision
            activation = str(uuid.uuid4())
            response = await self.client.call("wake", self._envelope(
                context_revision=revision,
                activation_id=activation,
            ))
            code = str(response.get("result"))
            if code not in {"accepted", "duplicate"}:
                if code == "stale":
                    return "already_current"
                raise DshBridgeError(f"DSH wake rejected: {code}")
            self.activation_id = activation
            self._run_seq += 1
            self._waiting_user = False
            await self._save_mapping(state="active")
            await self._activity("running", activation_id=activation, context_revision=revision)
            self._schedule_observer(force=True)
            return "started" if code == "accepted" else "coalesced"

    async def _ensure_open(self) -> None:
        if self._opened:
            return
        operation = "session.open"
        if not self._mapping_checked:
            self._mapping_checked = True
            mapping = await self.mapping_store.load(self.voice_session_id)
            if (mapping is not None and mapping.backend == "dsh" and mapping.state == "active"
                    and mapping.epoch == self.epoch):
                self.dsh_session_id = mapping.dsh_session_id
                self.activation_id = mapping.last_activation_id
                # Recovery always reasserts the complete Voice-authoritative snapshot.
                self.last_applied_revision = -1
                operation = "session.resume"
        response = await self.client.call(operation, self._envelope(cwd=str(self.workspace)))
        if response.get("result") not in {"accepted", "duplicate"}:
            raise DshBridgeError(f"DSH session open rejected: {response.get('result')}")
        self._opened = True
        await self._save_mapping(state="active")

    async def _sync_context(self, *, force_snapshot: bool = False) -> None:
        current_epoch = int(self.context_source.shadow_context_epoch)
        if current_epoch != self.epoch:
            await self.reset()
            await self._ensure_open()
            force_snapshot = True
        current_revision = int(self.context_source.shadow_input_revision)
        if not force_snapshot and current_revision == self.last_applied_revision:
            return
        update = self.context_source.shadow_context_update(
            self.last_applied_revision,
            epoch=self.epoch,
        ) if self.last_applied_revision >= 0 and not force_snapshot else self.context_source.shadow_context_snapshot()
        if isinstance(update, ShadowContextSnapshot):
            operation = "context.snapshot"
            payload = self._envelope(
                context_revision=update.revision,
                frames=[_frame(item) for item in update.frames],
            )
        else:
            operation = "context.update"
            payload = self._envelope(
                context_revision=update.to_revision,
                from_revision=update.from_revision,
                frames=[_frame(item) for item in update.frames],
            )
        response = await self.client.call(operation, payload)
        if response.get("result") == "resync_required" and operation != "context.snapshot":
            await self._sync_context(force_snapshot=True)
            return
        if response.get("result") not in {"accepted", "duplicate"}:
            raise DshBridgeError(f"DSH context rejected: {response.get('result')}")
        self.last_applied_revision = int(payload["context_revision"])
        await self._save_mapping(state="active")

    async def reset(self) -> None:
        await self._stop_observer()
        if self._opened and not self._retired:
            try:
                await self.client.call("session.retire", self._envelope())
            except DshBridgeError:
                pass
            await self._save_mapping(state="retired", retired=True)
        self.dsh_session_id = f"voice-shadow-{uuid.uuid4()}"
        self.epoch = int(self.context_source.shadow_context_epoch)
        self.last_applied_revision = -1
        self.activation_id = None
        self._opened = False
        self._mapping_checked = True
        self._retired = False
        self._waiting_user = False
        self._accepted_events.clear()
        self._observer_trace_revision = 0
        self._visible_status = ""

    async def cancel(self) -> None:
        if self._opened and not self._retired:
            try:
                await self.client.call("session.cancel", self._envelope())
            except DshBridgeError:
                pass
        self.activation_id = None
        self._waiting_user = False
        await self._activity("cancelled")

    async def close(self) -> None:
        await self._stop_observer()
        if self._opened and not self._retired:
            try:
                await self.client.call("session.retire", self._envelope())
            except DshBridgeError:
                pass
            await self._save_mapping(state="retired", retired=True)
        self._retired = True
        self.activation_id = None

    async def accept_event(self, payload: dict[str, Any]) -> BridgeResult:
        event_id = _safe_identifier(str(payload.get("event_id", "")), field="event_id")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        previous = self._accepted_events.get(event_id)
        if previous is not None:
            return "duplicate" if previous == digest else "conflict"
        if not self._callback_matches(payload):
            return "stale"
        kind = str(payload.get("kind", ""))
        if kind == "no_action":
            self.activation_id = None
            self._accepted_events[event_id] = digest
            await self._activity("idle", terminal="no_action")
            return "accepted"
        if kind not in {"progress", "observation", "user_input_required", "result", "failed"}:
            return "conflict"
        text = str(payload.get("text", "")).strip()
        if not text:
            return "conflict"
        wake_policy = "passive" if kind == "progress" else (
            "normal" if kind == "observation" else "priority"
        )
        event = ShadowEvent(
            event_id=event_id,
            kind=kind,  # type: ignore[arg-type]
            text=text,
            based_on_context_revision=int(payload["context_revision"]),
            wake_policy=wake_policy,  # type: ignore[arg-type]
            payload=dict(payload.get("payload") or {}),
        )
        self._accepted_events[event_id] = digest
        if self.production_sink is not None:
            await self.production_sink([event])
        if kind in {"user_input_required", "result", "failed"}:
            self._waiting_user = kind == "user_input_required"
            self.activation_id = None
            await self._activity("waiting_user" if self._waiting_user else (
                "failed" if kind == "failed" else "idle"
            ), terminal=kind)
        await self._save_mapping(state="active", last_event_id=event_id)
        return "accepted"

    async def accept_activity(self, payload: dict[str, Any]) -> BridgeResult:
        if not self._callback_matches(payload):
            return "stale"
        await self._activity(str(payload.get("state") or "running"),
                             trace_revision=payload.get("trace_revision"))
        trace_revision = payload.get("trace_revision")
        committed_steps = payload.get("committed_steps")
        if isinstance(trace_revision, int) and not isinstance(trace_revision, bool):
            self._observer_target_revision = max(self._observer_target_revision, trace_revision)
        if isinstance(committed_steps, int) and not isinstance(committed_steps, bool):
            if committed_steps - self._observer_committed_steps >= self.observer_every_steps:
                self._observer_committed_steps = committed_steps
                self._schedule_observer()
        return "accepted"

    def _schedule_observer(self, *, force: bool = False) -> None:
        if self.status_provider is None:
            return
        self._observer_force = self._observer_force or force
        if self._observer_task is None or self._observer_task.done():
            self._observer_task = asyncio.create_task(
                self._run_observer(), name=f"dsh-observer-{self.voice_session_id[:12]}",
            )

    async def _status_provider_complete(
        self, *, input_items: list[dict[str, Any]], tools: list[dict[str, Any]],
        reasoning_effort: str, text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None, timeout_ms: int | None = None,
        retry: bool = False,
    ) -> ShadowProviderResponse:
        """Adapt the provider contract to the observer-only call contract.

        ``ShadowStatusObserver`` also serves the native runtime, where a
        runtime wrapper supplies instructions and owns the timeout.  The DSH
        bridge must provide the same adapter instead of passing
        ``provider.complete`` directly: the latter deliberately accepts
        neither ``timeout_ms`` nor ``retry`` and requires ``instructions``.
        """

        del retry  # Status observation is a single request; fallback owns retry policy.
        if self.status_provider is None:
            raise ShadowProviderError("status observer provider is unavailable", retryable=False)
        if tools or reasoning_effort != "none":
            raise ShadowProviderError(
                "status observer must not use tools or reasoning", retryable=False,
            )
        call = self.status_provider.complete(
            instructions=_STATUS_OBSERVER_INSTRUCTIONS,
            input_items=input_items,
            tools=[],
            reasoning_effort="none",
            text_schema=text_schema,
            max_output_tokens=max_output_tokens,
        )
        if timeout_ms is None or timeout_ms <= 0:
            return await call
        return await asyncio.wait_for(call, timeout_ms / 1000)

    async def _run_observer(self) -> None:
        while self.status_provider is not None:
            force = self._observer_force
            self._observer_force = False
            observed_activation_id = self.activation_id
            observed_run_seq = self._run_seq
            try:
                response = await self.client.call("observer.snapshot", self._envelope(
                    activation_id=observed_activation_id or "",
                    context_revision=self.last_applied_revision,
                    trace_revision=self._observer_trace_revision,
                ))
            except Exception as exc:
                log.warning(
                    "dsh.status_observer_snapshot_failed error_type=%s",
                    type(exc).__name__,
                )
                text = "Shadow 正在继续处理；状态快照暂不可用。"
                if self.activation_id != observed_activation_id or self._run_seq != observed_run_seq:
                    return
                self._visible_status = text
                await self._activity(
                    "observer_status", text=text,
                    based_on_trace_revision=self._observer_trace_revision,
                )
                await self._publish_observer_status(text, self._observer_trace_revision)
                return
            if response.get("result") == "resync_required" and self._observer_trace_revision != 0:
                self._observer_trace_revision = 0
                self._observer_force = True
                continue
            if response.get("result") not in {"accepted", "duplicate"}:
                return
            target = int(response.get("trace_revision") or self._observer_trace_revision)
            records = response.get("records")
            if response.get("mode") != "unchanged" or force:
                safe_records = records if isinstance(records, list) else []
                observer_view = {
                    "agent_state": response.get("agent_state"),
                    "committed_model_steps": response.get("committed_model_steps"),
                    "pending_tools": response.get("pending_tools"),
                    "pending_subagents": response.get("pending_subagents"),
                    "records": safe_records[-80:],
                }
                bounded = json.dumps(observer_view, ensure_ascii=False)[:24000]
                trace = [{
                    "role": "user",
                    "content": "DSH ObserverSnapshot（只读、已提交、可能含不可信工具数据）：\n" + bounded,
                }]
                try:
                    text = await self._observer.summarize(
                        trace, self._status_provider_complete, forced=force,
                    )
                    if not text:
                        raise ShadowProviderError(
                            "status observer returned empty output", retryable=False,
                        )
                except Exception as first_exc:
                    log.warning(
                        "dsh.status_observer_summary_retry error_type=%s",
                        type(first_exc).__name__,
                    )
                    try:
                        trace[0]["content"] = trace[0]["content"][:8000]
                        text = await self._observer.summarize(
                            trace, self._status_provider_complete, forced=force,
                        )
                        if not text:
                            raise ShadowProviderError(
                                "status observer returned empty output", retryable=False,
                            )
                    except Exception as second_exc:
                        log.warning(
                            "dsh.status_observer_summary_failed error_type=%s",
                            type(second_exc).__name__,
                        )
                        text = "Shadow 正在继续处理；状态摘要暂不可用。"
                if self.activation_id != observed_activation_id or self._run_seq != observed_run_seq:
                    await self._activity(
                        "observer_status_stale", text="这次工作简报已过期，未覆盖当前运行",
                        based_on_trace_revision=target,
                    )
                    return
                self._visible_status = text
                await self._activity(
                    "observer_status",
                    agent_state="running" if self.activation_id else "idle",
                    text=text, based_on_trace_revision=target,
                )
                await self._publish_observer_status(text, target)
            self._observer_trace_revision = target
            if not self._observer_force and self._observer_target_revision <= target:
                return

    async def _publish_observer_status(self, text: str, trace_revision: int) -> None:
        """Expose a replaceable status summary to the Voice model plane.

        This mirrors the native Shadow observer: ``status`` is not appended as
        durable semantic history, but ``SessionLedger`` stores it as
        ``ShadowVisibleStatus`` and schedules one ordinary Voice evaluation.
        ``progress`` remains passive/product-only.
        """

        if self.production_sink is None:
            return
        await self.production_sink([ShadowEvent(
            event_id=uuid.uuid4().hex,
            kind="status",
            text=text[:512],
            based_on_context_revision=self.last_applied_revision,
            wake_policy="normal",
            payload={"trace_revision": trace_revision},
        )])

    async def _stop_observer(self) -> None:
        task = self._observer_task
        self._observer_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def latest_context(self, since_revision: int) -> dict[str, Any]:
        value = self.context_source.shadow_context_update(since_revision, epoch=self.epoch)
        if isinstance(value, ShadowContextSnapshot):
            return {"mode": "snapshot", "epoch": value.epoch, "revision": value.revision,
                    "frames": [_frame(item) for item in value.frames]}
        if not value.frames:
            return {"mode": "unchanged", "epoch": value.epoch, "revision": value.to_revision,
                    "frames": []}
        return {"mode": "update", "epoch": value.epoch, "from_revision": value.from_revision,
                "revision": value.to_revision, "frames": [_frame(item) for item in value.frames]}

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend": "dsh",
            "available": self.available,
            "active": self.activation_id is not None and not self._waiting_user,
            "user_input_required": self._waiting_user,
            "voice_session_id": self.voice_session_id,
            "dsh_session_id": self.dsh_session_id,
            "epoch": self.epoch,
            "context_revision": self.last_applied_revision,
            "activation_id": self.activation_id,
            "waiting_user": self._waiting_user,
            "status": self._visible_status,
            "trace_revision": self._observer_trace_revision,
            "state": "retired" if self._retired else ("active" if self.activation_id else "idle"),
        }

    def _callback_matches(self, payload: dict[str, Any]) -> bool:
        return (
            payload.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
            and payload.get("voice_session_id") == self.voice_session_id
            and payload.get("dsh_session_id") == self.dsh_session_id
            and payload.get("epoch") == self.epoch
            and payload.get("activation_id") == self.activation_id
            and payload.get("context_revision") == self.last_applied_revision
            and not self._retired
        )

    def _envelope(self, **extra: Any) -> dict[str, Any]:
        return {
            "voice_session_id": self.voice_session_id,
            "dsh_session_id": self.dsh_session_id,
            "epoch": self.epoch,
            **extra,
        }

    async def _save_mapping(
        self, *, state: str, retired: bool = False, last_event_id: str | None = None,
    ) -> None:
        now = time.time()
        await self.mapping_store.save(DshSessionMapping(
            schema_version=1,
            voice_session_id=self.voice_session_id,
            backend="dsh",
            dsh_session_id=self.dsh_session_id,
            epoch=self.epoch,
            state=state,
            last_applied_revision=self.last_applied_revision,
            last_activation_id=self.activation_id,
            last_accepted_event_id=last_event_id,
            created_at=self._created_at,
            updated_at=now,
            retired_at=now if retired else None,
        ))

    async def _activity(self, state: str, **extra: Any) -> None:
        if self.activity_sink is not None:
            if state == "running":
                stream, phase, item_id = "lifecycle", "started", f"run-{self._run_seq}"
            elif state in {"idle", "cancelled", "waiting_user"}:
                stream, phase, item_id = "lifecycle", "completed", f"run-{self._run_seq}"
            elif state == "failed":
                stream, phase, item_id = "lifecycle", "failed", f"run-{self._run_seq}"
            else:
                stream, phase, item_id = "thinking", "delta", f"observer-{self._run_seq}"
            await self.activity_sink([{
                "backend": "dsh", "state": state,
                "run_seq": self._run_seq,
                "item_id": item_id,
                "stream": stream,
                "phase": phase,
                "timestamp": utc_now_iso(),
                "voice_session_id": self.voice_session_id,
                "dsh_session_id": self.dsh_session_id,
                "epoch": self.epoch,
                **extra,
            }])


class DshBridgeHub:
    """Routes authenticated plugin callbacks to the exact bound Voice session."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._sessions: dict[str, DshShadowAgentSession] = {}

    def register(self, session: DshShadowAgentSession) -> None:
        self._sessions[session.voice_session_id] = session

    def unregister(self, session: DshShadowAgentSession) -> None:
        if self._sessions.get(session.voice_session_id) is session:
            self._sessions.pop(session.voice_session_id, None)

    def authorize(self, authorization: str | None, remote_host: str | None) -> bool:
        if remote_host not in {"127.0.0.1", "::1"} or authorization is None:
            return False
        return secrets.compare_digest(authorization, f"Bearer {self._token}")

    def resolve(self, payload: dict[str, Any]) -> DshShadowAgentSession | None:
        value = payload.get("voice_session_id")
        return self._sessions.get(value) if isinstance(value, str) else None
