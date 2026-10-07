"""Authenticated Voice Terminal side of the first-party DSH Shadow Bridge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
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
from ..shadow_agent.projection import _text_from_output


BRIDGE_PROTOCOL_VERSION = "2.0"
log = logging.getLogger(__name__)

_STATUS_OBSERVER_INSTRUCTIONS = (
    "You are the isolated, read-only Voice Terminal Shadow status observer. "
    "Summarize only the committed ObserverSnapshot supplied in the request. "
    "Never use tools, continue the task, or expose private reasoning."
)
_DSH_CONTEXT_GATE_PROMPT = (
    "你是 DSH Shadow 的侧载引导时机判断器。候选权威正文已经同步到 DSH；"
    "只判断是否要在当前激活中主动引导代理注意它。补充事实、条件变化、意图偏移、"
    "用户回答及不确定情况一律 inject_now；只有明确无关或完全重复时才可 defer_once。"
    "defer_once 仅推迟本次引导，下一次请求可以重新判断同一区间。"
    "不得改写正文、执行任务、生成指令或控制语音；严格按给定 JSON Schema 输出。"
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

    def __init__(
        self, message: str, *, operation: str | None = None,
        request_id: str | None = None, transport: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.request_id = request_id
        self.transport = bool(transport)


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
    raw = str(value)
    value = raw.strip()
    if not value or len(value) > 160 or any(char in value for char in "\r\n\0"):
        raise DshBridgeError(f"invalid {field}")
    if value != raw:
        raise DshBridgeError(f"invalid {field}")
    return value


def _strict_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DshBridgeError(f"invalid {field}")
    text = value.strip()
    if not text or len(text) > maximum:
        raise DshBridgeError(f"invalid {field}")
    return text


def _strict_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Bridge 2.0 discriminated event without compatibility aliases."""

    allowed_envelope = {
        "protocol_version", "request_id", "event_id", "voice_session_id",
        "dsh_session_id", "epoch", "context_revision", "activation_id", "event",
    }
    if set(payload) != allowed_envelope:
        raise DshBridgeError("invalid shadow.event envelope")
    if payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
        raise DshBridgeError("unsupported protocol version")
    for field in (
        "request_id", "event_id", "voice_session_id", "dsh_session_id", "activation_id",
    ):
        value = payload.get(field)
        if not isinstance(value, str):
            raise DshBridgeError(f"invalid {field}")
        _safe_identifier(value, field=field)
    for field, minimum in (("epoch", 0), ("context_revision", 0)):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise DshBridgeError(f"invalid {field}")
    raw = payload.get("event")
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise DshBridgeError("invalid event")
    event_type = raw["type"]
    if event_type == "progress":
        expected = {"type", "text"}
        event = {"type": event_type, "text": _strict_text(raw.get("text"), field="text", maximum=512)}
    elif event_type == "information":
        expected = {"type", "text"}
        event = {"type": event_type, "text": _strict_text(raw.get("text"), field="text", maximum=2000)}
    elif event_type == "input_required":
        expected = {"type", "question", "options"}
        options = raw.get("options")
        if not isinstance(options, list) or len(options) > 8:
            raise DshBridgeError("invalid options")
        event = {
            "type": event_type,
            "question": _strict_text(raw.get("question"), field="question", maximum=1000),
            "options": [
                _strict_text(item, field=f"options[{index}]", maximum=200)
                for index, item in enumerate(options)
            ],
        }
    elif event_type == "result":
        expected = {"type", "text"}
        event = {"type": event_type, "text": _strict_text(raw.get("text"), field="text", maximum=8000)}
    elif event_type == "no_action":
        expected = {"type"} if "reason" not in raw else {"type", "reason"}
        event = {"type": event_type}
        if "reason" in raw:
            event["reason"] = _strict_text(raw.get("reason"), field="reason", maximum=500)
    elif event_type == "failed":
        expected = {"type", "message", "code", "infrastructure"}
        if not isinstance(raw.get("infrastructure"), bool):
            raise DshBridgeError("invalid infrastructure")
        event = {
            "type": event_type,
            "message": _strict_text(raw.get("message"), field="message", maximum=2000),
            "code": _strict_text(raw.get("code"), field="code", maximum=128),
            "infrastructure": raw["infrastructure"],
        }
    else:
        raise DshBridgeError("unknown event type")
    if set(raw) != expected:
        raise DshBridgeError("event contains unknown or inapplicable fields")
    return event


@dataclass
class DshSessionMapping:
    schema_version: int  # 映射结构版本：固定为 2，不读取旧结构。
    voice_session_id: str  # Voice 会话标识：本地语音会话的主键。
    backend: str  # 后端类型：当前固定为 dsh。
    dsh_session_id: str  # DSH 会话标识：同一语音会话跨进程复用的绑定。
    epoch: int  # 会话代次：与 Voice 权威上下文一致。
    state: str  # 映射状态：active、idle、waiting_user、failed 或 retired。
    last_applied_revision: int  # 已应用修订号：DSH 已同步的最新 Voice 上下文版本。
    last_activation_id: str | None  # 当前激活标识：无活动任务时为空。
    last_terminal_activation_id: str | None  # 最后终态激活标识：供重启后确认终态重放。
    last_terminal_dsh_session_id: str | None  # 终态来源 DSH 会话标识：供旧绑定回执校验。
    last_terminal_event_id: str | None  # 最后终态事件标识：限制仅确认最近一条终态重放。
    last_terminal_event_digest: str | None  # 终态正文摘要：判断重放正文是否完全一致。
    created_at: float  # 创建时间：本地映射创建时间戳。
    updated_at: float  # 更新时间：本地映射最后写入时间戳。
    retired_at: float | None = None  # 退役时间：绑定退役时记录的时间戳。
    gate_queued_revision: int = 0  # 已排队引导版本：与已同步版本分离。
    gate_claimed_revision: int = 0  # 已领取引导版本：供恢复与诊断使用。
    gate_deferred_revision: int = 0  # 暂缓版本：重启后从权威正文重建下一激活。


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
        if not isinstance(value, dict) or value.get("schema_version") != 2:
            raise DshBridgeError("DSH mapping schema is unsupported")
        sessions = value.get("sessions")
        if not isinstance(sessions, dict):
            raise DshBridgeError("DSH mapping state is malformed")
        return sessions

    def _write(self, sessions: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            {"schema_version": 2, "sessions": sessions},
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
        request_id = str(payload.get("request_id") or uuid.uuid4())
        body = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "request_id": request_id,
            **payload,
        }
        started = time.monotonic()
        try:
            response = await self._request("POST", operation, body)
        except DshBridgeError as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            log.warning(
                "dsh.bridge operation=%s request_id=%s context_revision=%s "
                "activation_id=%s elapsed_ms=%s result=error transport=%s error=%s",
                operation, request_id, body.get("context_revision"),
                body.get("activation_id"), elapsed_ms, exc.transport, exc,
            )
            if exc.operation is None:
                exc.operation = operation
            if exc.request_id is None:
                exc.request_id = request_id
            raise
        if response.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
            raise DshBridgeError(
                "DSH Bridge protocol mismatch", operation=operation,
                request_id=request_id,
            )
        log.info(
            "dsh.bridge operation=%s request_id=%s context_revision=%s "
            "activation_id=%s elapsed_ms=%s result=%s",
            operation, request_id, body.get("context_revision"),
            body.get("activation_id"),
            round((time.monotonic() - started) * 1000, 1), response.get("result"),
        )
        return response

    async def task_api(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the DSH-owned task catalog（DSH 管理的任务目录）."""
        return await asyncio.to_thread(self._task_api_sync, method, path, payload)

    def _task_api_sync(
        self, method: str, path: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not path.startswith("tasks") or ".." in path:
            raise DshBridgeError("invalid task path")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}/api/voice-terminal-wake/v1/{path}",
            data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                value = json.loads(response.read(2 * 1024 * 1024 + 1))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(8192)).get("detail", "任务请求失败。")
            except (ValueError, AttributeError):
                detail = "任务请求失败。"
            error = DshBridgeError(str(detail)[:500])
            error.status_code = exc.code
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DshBridgeError(f"DSH task API failed: {type(exc).__name__}") from None
        if not isinstance(value, dict):
            raise DshBridgeError("DSH task API returned a non-object response")
        return value

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
                # session.export（完整会话导出）按记录数分页；单个工具结果不能按字节截断。
                raw = response.read() if operation == "session.export" else response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(2 * 1024 * 1024 + 1)
            if 400 <= exc.code < 500 and len(raw) <= 2 * 1024 * 1024:
                try:
                    rejected = json.loads(raw)
                except ValueError:
                    rejected = None
                if (isinstance(rejected, dict)
                        and rejected.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
                        and isinstance(rejected.get("result"), str)):
                    return rejected
            request_id = str((payload or {}).get("request_id") or "")
            raise DshBridgeError(
                f"DSH Bridge HTTP failure: {exc.code}", operation=operation,
                request_id=request_id, transport=exc.code >= 500,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            request_id = str((payload or {}).get("request_id") or "")
            raise DshBridgeError(
                f"DSH Bridge transport failed: {type(exc).__name__}",
                operation=operation, request_id=request_id, transport=True,
            ) from None
        if operation != "session.export" and len(raw) > 2 * 1024 * 1024:
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
        gate_provider: ShadowProvider | None = None,
        observer_every_steps: int = 5,
        observer_max_output_tokens: int = 512,
        observer_timeout_ms: int | None = None,
        availability_waiter: AvailabilityWaiter | None = None,
        runtime_reminder_interval_ms: int = 20000,
    ) -> None:
        if runtime_reminder_interval_ms <= 0:
            raise ValueError("runtime_reminder_interval_ms must be positive")
        self.client = client
        self.mapping_store = mapping_store
        self.context_source = context_source
        self.voice_session_id = _safe_identifier(voice_session_id, field="voice_session_id")
        self.workspace = workspace.resolve()
        self.production_sink = production_sink
        self.activity_sink = activity_sink
        self.status_provider = status_provider
        self.gate_provider = gate_provider or status_provider
        self.availability_waiter = availability_waiter
        self.runtime_reminder_interval_ms = int(runtime_reminder_interval_ms)
        self.observer_every_steps = max(1, int(observer_every_steps))
        self._observer = ShadowStatusObserver(
            max_output_tokens=observer_max_output_tokens,
            timeout_ms=observer_timeout_ms,
        )
        self.dsh_session_id = f"voice-shadow-{uuid.uuid4()}"
        self.epoch = int(context_source.shadow_context_epoch)
        self.last_applied_revision = -1
        self.last_guided_revision = 0
        self.last_claimed_revision = 0
        self._deferred_revision = 0
        self._gate_requested_revision = 0
        self._gate_enabled = False
        self._gate_generation = 0
        self._handoff_task: asyncio.Task[None] | None = None
        self.activation_id: str | None = None
        self._opened = False
        self._retired = False
        self._wake_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()
        self._accepted_events: dict[str, str] = {}
        self._accepted_requests: dict[str, str] = {}
        self._accepted_early_silence: dict[str, tuple[str, str]] = {}  # 提前静默受理记录：请求标识对应激活身份与受理状态。
        self._last_terminal_activation_id: str | None = None
        self._last_terminal_dsh_session_id: str | None = None
        self._last_terminal_event_id: str | None = None
        self._last_terminal_event_digest: str | None = None
        self._waiting_user = False
        self._created_at = time.time()
        self._mapping_checked = False
        self._binding_restore_checked = False  # 绑定恢复检查：独立于终态回执的加载。
        self._resume_existing = False  # 已有会话恢复：重连使用持久身份而非再次创建。
        self._observer_trace_revision = 0
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_target_revision = 0
        self._observer_force = False
        self._visible_status = ""
        self._run_seq = 0
        self._observer_committed_steps = 0
        self._context_resync_task: asyncio.Task[None] | None = None
        self._telemetry: dict[str, int | float] = {}  # telemetry（桥接插件会话用量）：不包含正文或凭据。
        self._telemetry_trace_revision = -1  # telemetry_trace_revision（用量水位）：防止迟到快照覆盖新用量。

    @property
    def available(self) -> bool:
        return not self._retired

    def set_context_gate_enabled(self, enabled: bool) -> bool:
        value = bool(enabled)
        if self._gate_enabled != value:
            self._gate_enabled = value
            self._gate_generation += 1
            log.info("dsh.context_gate toggle voice_session_id=%s enabled=%s generation=%s",
                     self.voice_session_id, value, self._gate_generation)
        return self._gate_enabled

    async def resume_pending_gate(self) -> None:
        mapping = await self.mapping_store.load(self.voice_session_id)
        if mapping is None or mapping.backend != "dsh" or mapping.epoch != self.epoch:
            return
        if mapping.gate_deferred_revision <= 0:
            return
        self.last_guided_revision = mapping.gate_queued_revision
        self.last_claimed_revision = mapping.gate_claimed_revision
        self._deferred_revision = mapping.gate_deferred_revision
        log.info("dsh.context_gate restore voice_session_id=%s queued_revision=%s deferred_revision=%s",
                 self.voice_session_id, self.last_guided_revision,
                 self._deferred_revision)
        self._schedule_handoff()

    async def flush_context_gate(self) -> None:
        """Release one pending interval before the disabled path resumes."""
        async with self._wake_lock:
            if not self._opened:
                return
            if int(self.context_source.shadow_input_revision) > self.last_applied_revision:
                await self._sync_context(hold=(
                    self.last_applied_revision > self.last_guided_revision
                    or self._deferred_revision > 0
                ))
            if self.last_applied_revision <= self.last_guided_revision:
                return
            target = self.last_applied_revision
            if self.activation_id is not None:
                await self._send_guidance("inject_now", target, self.activation_id)
                self.last_guided_revision = target
                self._deferred_revision = 0
                await self._save_mapping(state="active")
                log.info("dsh.context_gate released_on_disable voice_session_id=%s activation_id=%s queued_revision=%s target=next_step",
                         self.voice_session_id, self.activation_id, target)
                return
            self._deferred_revision = target
            await self._save_mapping(state="idle")
        await self.request_wake()

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

    async def prepare_callback_binding(self, payload: dict[str, Any]) -> None:
        """Restore only a version-2 binding/terminal receipt before callback routing."""

        if self._mapping_checked:
            return
        async with self._event_lock:
            if self._mapping_checked:
                return
            mapping = await self.mapping_store.load(self.voice_session_id)
            self._mapping_checked = True
            if (
                mapping is None or mapping.backend != "dsh" or mapping.epoch != self.epoch
                or not (
                    payload.get("dsh_session_id") == mapping.dsh_session_id
                    or payload.get("dsh_session_id") == mapping.last_terminal_dsh_session_id
                )
            ):
                return
            self._last_terminal_activation_id = mapping.last_terminal_activation_id
            self._last_terminal_dsh_session_id = mapping.last_terminal_dsh_session_id
            self._last_terminal_event_id = mapping.last_terminal_event_id
            self._last_terminal_event_digest = mapping.last_terminal_event_digest

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
            was_active = self.activation_id is not None
            deferred_handoff = not was_active and self._deferred_revision > 0
            if was_active and self._gate_enabled:
                self._gate_requested_revision = max(
                    self._gate_requested_revision,
                    int(self.context_source.shadow_input_revision),
                )
            try:
                await self._sync_context(hold=self._gate_enabled and was_active or deferred_handoff)
            except DshBridgeError as exc:
                if not was_active or not exc.transport:
                    raise
                log.warning(
                    "dsh.context_sync deferred voice_session_id=%s activation_id=%s "
                    "local_revision=%s target_revision=%s error=%s",
                    self.voice_session_id, self.activation_id,
                    self.last_applied_revision,
                    int(self.context_source.shadow_input_revision), exc,
                )
                self._schedule_context_resync()
                return "coalesced"
            deferred_handoff = self.activation_id is None and self._deferred_revision > 0
            if was_active and self.activation_id is None and self.last_applied_revision > self.last_guided_revision:
                self._deferred_revision = max(self._deferred_revision, self.last_applied_revision)
                deferred_handoff = True
            if self.activation_id is not None:
                if self._gate_enabled:
                    await self._judge_running_context()
                return "coalesced"
            if deferred_handoff and not await self._wait_handoff_idle():
                return "coalesced"
            revision = self.last_applied_revision
            activation = str(uuid.uuid4())
            async with self._event_lock:
                response = await self._call_reconciled("wake", self._envelope(
                    request_id=str(uuid.uuid4()),
                    context_revision=revision,
                    activation_id=activation,
                    **({"guidance_delivery": "deferred_next_turn"} if deferred_handoff else {}),
                ), expected_activation_id=activation)
                code = str(response.get("result"))
                if code not in {"accepted", "duplicate"}:
                    if code == "stale":
                        return "already_current"
                    raise DshBridgeError(f"DSH wake rejected: {code}")
                self.activation_id = activation
                if deferred_handoff:
                    log.info("dsh.context_gate handoff voice_session_id=%s old_activation_id=%s new_activation_id=%s queued_revision=%s",
                             self.voice_session_id, self._last_terminal_activation_id,
                             activation, revision)
                    self.last_guided_revision = revision
                    self._deferred_revision = 0
                    self._gate_requested_revision = 0
                else:
                    self.last_guided_revision = max(self.last_guided_revision, revision)
                self._run_seq += 1
                self._waiting_user = False
                self._visible_status = ""
                await self._save_mapping(state="active")
            await self._activity("running", activation_id=activation, context_revision=revision)
            self._schedule_observer(force=True)
            return "started" if code == "accepted" else "coalesced"

    async def _wait_handoff_idle(self) -> bool:
        deadline = asyncio.get_running_loop().time() + 30
        while self.available and self._deferred_revision:
            try:
                state = await self._session_state()
                if state.get("ready_for_handoff") is True:
                    return True
            except DshBridgeError as exc:
                log.warning("dsh.context_gate handoff_state_failed voice_session_id=%s error_type=%s",
                            self.voice_session_id, type(exc).__name__)
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("dsh.context_gate handoff_wait_timeout voice_session_id=%s deferred_revision=%s",
                            self.voice_session_id, self._deferred_revision)
                return False
            await asyncio.sleep(0.1)
        return False

    async def _send_guidance(self, timing: str, revision: int, activation_id: str) -> None:
        payload = self._envelope(
            request_id=str(uuid.uuid4()), activation_id=activation_id,
            context_revision=revision, timing=timing,
        )
        for attempt in range(2):
            try:
                response = await self.client.call("context.guidance", payload)
                code = response.get("result")
                if code == "duplicate" and isinstance(response.get("original"), dict):
                    code = response["original"].get("result")
                if code == "accepted":
                    return
                raise DshBridgeError(f"DSH context guidance rejected: {code}")
            except DshBridgeError as exc:
                if not exc.transport:
                    raise
                try:
                    state = await self._session_state()
                    key = "deferred_revision" if timing == "defer_once" else "queued_revision"
                    if int(state.get(key) or 0) >= revision:
                        return
                except DshBridgeError:
                    pass
                if attempt:
                    raise

    async def _judge_running_context(self) -> None:
        target = self.last_applied_revision
        if target <= self.last_guided_revision:
            return
        activation_id = self.activation_id
        if activation_id is None:
            return
        generation = self._gate_generation
        epoch = self.epoch
        raw = self.context_source.shadow_context_update(
            self.last_guided_revision, epoch=epoch,
        )
        frames = tuple(frame for frame in raw.frames if self.last_guided_revision < frame.revision <= target)
        started = time.monotonic()
        timing = "inject_now"
        relation = "uncertain"
        failure = ""
        log.info("dsh.context_gate requested voice_session_id=%s activation_id=%s from_revision=%s target_revision=%s frame_count=%s",
                 self.voice_session_id, activation_id, self.last_guided_revision,
                 target, len(frames))
        try:
            if not frames:
                raise ValueError("empty_interval")
            if self.gate_provider is None:
                raise ValueError("provider_unavailable")
            snapshot = await self.client.call("observer.snapshot", self._envelope(
                request_id=str(uuid.uuid4()), activation_id=activation_id,
                context_revision=target, trace_revision=0,
            ))
            records = snapshot.get("records")
            if snapshot.get("result") != "accepted" or not isinstance(records, list):
                raise ValueError("snapshot_unavailable")
            starts = [index for index, item in enumerate(records)
                      if isinstance(item, dict) and item.get("type") == "voice/activation"
                      and isinstance(item.get("data"), dict)
                      and item["data"].get("activation_id") == activation_id]
            if not starts:
                raise ValueError("activation_trace_unavailable")
            current_records = records[starts[-1] + 1:][-80:]
            status = self._visible_status
            if not current_records and not status:
                raise ValueError("current_status_unavailable")
            schema = {
                "type": "object", "additionalProperties": False,
                "required": ["timing", "relation", "reason"],
                "properties": {
                    "timing": {"type": "string", "enum": ["inject_now", "defer_once"]},
                    "relation": {"type": "string", "enum": [
                        "supplement", "change", "deviation", "guidance",
                        "irrelevant", "duplicate", "uncertain",
                    ]},
                    "reason": {"type": "string", "maxLength": 240},
                },
            }
            body = {"shadow_summary": status,
                    "committed_trace": current_records,
                    "candidate": {"from_revision": self.last_guided_revision,
                                  "to_revision": target,
                                  "frames": [_frame(item) for item in frames]}}
            serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) > 24000:
                raise ValueError("candidate_too_large")
            response = await asyncio.wait_for(self.gate_provider.complete(
                instructions=_DSH_CONTEXT_GATE_PROMPT,
                input_items=[{"role": "user", "content": serialized}],
                tools=[], reasoning_effort="none", text_schema=schema,
                max_output_tokens=256,
            ), timeout=15)
            verdict = json.loads(_text_from_output(response.output))
            if not isinstance(verdict, dict) or set(verdict) != {"timing", "relation", "reason"}:
                raise ValueError("invalid_verdict")
            relation = str(verdict["relation"])
            if relation not in {"supplement", "change", "deviation", "guidance", "irrelevant", "duplicate", "uncertain"}:
                raise ValueError("invalid_relation")
            if not isinstance(verdict["reason"], str) or len(verdict["reason"]) > 240:
                raise ValueError("invalid_reason")
            if verdict["timing"] == "defer_once" and relation in {"irrelevant", "duplicate"}:
                timing = "defer_once"
            elif verdict["timing"] != "inject_now":
                raise ValueError("invalid_timing")
            if relation == "uncertain":
                failure = "uncertain"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = type(exc).__name__ if not isinstance(exc, ValueError) else str(exc)
            timing = "inject_now"
        elapsed = round((time.monotonic() - started) * 1000, 2)
        if epoch != self.epoch or generation != self._gate_generation or activation_id != self.activation_id:
            log.info("dsh.context_gate stale voice_session_id=%s old_activation_id=%s current_activation_id=%s target_revision=%s elapsed_ms=%s",
                     self.voice_session_id, activation_id, self.activation_id, target, elapsed)
            if epoch != self.epoch or not self._opened or self._retired or (
                generation != self._gate_generation and self.activation_id is None
                and self._deferred_revision == 0
            ):
                return
            if self.activation_id is None:
                self._deferred_revision = max(self._deferred_revision, target)
                self._schedule_handoff()
                return
            timing = "inject_now"
            activation_id = self.activation_id
        if failure:
            log.warning("dsh.context_gate fail_open voice_session_id=%s activation_id=%s target_revision=%s reason=%s elapsed_ms=%s",
                        self.voice_session_id, activation_id, target, failure, elapsed)
        await self._send_guidance(timing, target, activation_id)
        if timing == "inject_now":
            self.last_guided_revision = target
            self._deferred_revision = 0
            if self._gate_requested_revision <= target:
                self._gate_requested_revision = 0
        else:
            self._deferred_revision = target
        await self._save_mapping(state="active")
        log.info("dsh.context_gate decided voice_session_id=%s activation_id=%s from_revision=%s target_revision=%s timing=%s relation=%s elapsed_ms=%s",
                 self.voice_session_id, activation_id,
                 raw.from_revision if isinstance(raw, ShadowContextUpdate) else 0,
                 target, timing, relation, elapsed)

    async def reconnect(self) -> None:
        """Rebind bridge（重新绑定桥接）并同步上下文，不重新派发历史动作。"""
        async with self._wake_lock:
            self._resume_existing = self._opened or self._resume_existing
            self._opened = False
            await self._ensure_open()
            await self._sync_context(force_snapshot=True, hold=True)

    async def _ensure_open(self) -> None:
        if self._opened:
            return
        operation = "session.open"
        if not self._binding_restore_checked:
            mapping = await self.mapping_store.load(self.voice_session_id)
            if mapping is not None and mapping.backend == "dsh" and mapping.epoch == self.epoch:
                if mapping.state != "retired":
                    self.dsh_session_id = mapping.dsh_session_id
                    self._created_at = mapping.created_at
                    self._resume_existing = True
                    self._waiting_user = mapping.state == "waiting_user"
                self._last_terminal_activation_id = mapping.last_terminal_activation_id
                self._last_terminal_dsh_session_id = mapping.last_terminal_dsh_session_id
                self._last_terminal_event_id = mapping.last_terminal_event_id
                self._last_terminal_event_digest = mapping.last_terminal_event_digest
                self._deferred_revision = max(self._deferred_revision, mapping.gate_deferred_revision)
                if mapping.gate_deferred_revision:
                    self.last_guided_revision = mapping.gate_queued_revision
                    self.last_claimed_revision = mapping.gate_claimed_revision
            elif mapping is not None and mapping.backend == "dsh" and mapping.state != "retired":
                raise DshBridgeError("DSH（后台代理）会话代次与语音恢复状态不一致，保留旧绑定")
            self._binding_restore_checked = True
            self._mapping_checked = True
        if self._resume_existing:
            state = await self.client.call("session.state", self._envelope())
            if state.get("result") in {"accepted", "duplicate"}:
                if state.get("state") == "retired":
                    raise DshBridgeError("DSH（后台代理）旧绑定已经退役，不能恢复为活动会话")
                self.last_applied_revision = int(state.get("context_revision", 0))
                self.activation_id = state.get("activation_id") or None
                self._opened = True
                self._notify_runtime_state()
                await self._save_mapping(state="active" if self.activation_id else "waiting_user" if self._waiting_user else "idle")
                return
            if state.get("result") != "not_found":
                raise DshBridgeError(f"DSH（后台代理）绑定恢复状态异常：{state.get('result')}")
            operation = "session.resume"
        response = await self._call_reconciled(
            operation,
            self._envelope(
                request_id=str(uuid.uuid4()), cwd=str(self.workspace),
                runtime_reminder_interval_ms=self.runtime_reminder_interval_ms,
            ),
            expected_context_revision=0,
        )
        if response.get("result") not in {"accepted", "duplicate"}:
            raise DshBridgeError(f"DSH session open rejected: {response.get('result')}")
        self._opened = True
        self._resume_existing = True
        # 新宿主只恢复持久历史；旧进程的激活和上下文水位不能沿用。
        self.activation_id = None
        self.last_applied_revision = -1
        self._notify_runtime_state()
        await self._save_mapping(state="waiting_user" if self._waiting_user else "idle")

    async def _sync_context(self, *, force_snapshot: bool = False, hold: bool = False) -> None:
        current_epoch = int(self.context_source.shadow_context_epoch)
        if current_epoch != self.epoch:
            await self.reset()
            await self._ensure_open()
            force_snapshot = True
            hold = False
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
        payload["request_id"] = str(uuid.uuid4())
        if hold:
            payload["guidance_delivery"] = "hold"
        async with self._event_lock:
            response = await self._call_reconciled(
                operation, payload, expected_context_revision=int(payload["context_revision"]),
            )
            result = response.get("result")
            if result not in {"accepted", "duplicate", "resync_required"}:
                raise DshBridgeError(f"DSH context rejected: {result}")
            if result != "resync_required":
                self.last_applied_revision = int(payload["context_revision"])
                if not hold:
                    self.last_guided_revision = max(self.last_guided_revision, self.last_applied_revision)
                elif self._gate_enabled:
                    log.info("dsh.context_gate synced voice_session_id=%s activation_id=%s from_revision=%s synced_revision=%s queued_revision=%s delivery=hold",
                             self.voice_session_id, self.activation_id,
                             payload.get("from_revision", 0), self.last_applied_revision,
                             self.last_guided_revision)
                mapping_state = "active" if self.activation_id is not None else (
                    "waiting_user" if self._waiting_user else "idle"
                )
                await self._save_mapping(state=mapping_state)
        if result == "resync_required" and operation != "context.snapshot":
            await self._sync_context(force_snapshot=True, hold=hold)
            return
        if result == "resync_required":
            raise DshBridgeError("DSH context snapshot requires resynchronization")

    async def _session_state(self) -> dict[str, Any]:
        response = await self.client.call(
            "session.state", self._envelope(request_id=str(uuid.uuid4())),
        )
        if response.get("result") != "accepted":
            raise DshBridgeError(
                f"DSH state rejected: {response.get('result')}",
                operation="session.state",
            )
        return response

    @staticmethod
    def _state_confirms(
        state: dict[str, Any], *, expected_context_revision: int | None,
        expected_activation_id: str | None,
    ) -> bool:
        if expected_activation_id is not None:
            return str(state.get("activation_id") or "") == expected_activation_id
        if expected_context_revision is not None:
            revision = state.get("context_revision")
            return (
                isinstance(revision, int) and not isinstance(revision, bool)
                and revision >= expected_context_revision
            )
        return False

    async def _call_reconciled(
        self, operation: str, payload: dict[str, Any], *,
        expected_context_revision: int | None = None,
        expected_activation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retry one ambiguous transport loss with a stable request identity."""

        try:
            return await self.client.call(operation, payload)
        except DshBridgeError as first:
            if not first.transport:
                raise
        for attempt in ("state_before_retry", "state_after_retry"):
            try:
                state = await self._session_state()
            except DshBridgeError:
                state = {}
            if self._state_confirms(
                state,
                expected_context_revision=expected_context_revision,
                expected_activation_id=expected_activation_id,
            ):
                log.info(
                    "dsh.bridge reconciled operation=%s request_id=%s via=%s "
                    "context_revision=%s activation_id=%s",
                    operation, payload.get("request_id"), attempt,
                    expected_context_revision, expected_activation_id,
                )
                return {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted"}
            if attempt == "state_before_retry":
                try:
                    retried = await self.client.call(operation, payload)
                    if retried.get("result") == "duplicate":
                        original = retried.get("original")
                        if isinstance(original, dict):
                            return original
                    return retried
                except DshBridgeError as retry_error:
                    if not retry_error.transport:
                        raise
                    first = retry_error
        raise first

    def _schedule_context_resync(self) -> None:
        if self._context_resync_task is not None and not self._context_resync_task.done():
            return

        async def resync() -> None:
            await asyncio.sleep(0.25)
            try:
                async with self._wake_lock:
                    if self.available and self.activation_id is not None:
                        await self._sync_context(hold=self._gate_enabled)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "dsh.context_sync background_failed voice_session_id=%s "
                    "activation_id=%s error=%s",
                    self.voice_session_id, self.activation_id, exc,
                )

        self._context_resync_task = asyncio.create_task(
            resync(), name=f"dsh-context-resync-{self.voice_session_id[:12]}",
        )

    def _schedule_handoff(self) -> None:
        if self._handoff_task is not None and not self._handoff_task.done():
            return

        async def drain() -> None:
            while self.available and self._deferred_revision > 0 and self.activation_id is None:
                try:
                    await self.request_wake()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("dsh.context_gate handoff_retry voice_session_id=%s deferred_revision=%s error_type=%s",
                                self.voice_session_id, self._deferred_revision, type(exc).__name__)
                if self._deferred_revision > 0:
                    await asyncio.sleep(1)

        self._handoff_task = asyncio.create_task(
            drain(), name=f"dsh-gate-handoff-{self.voice_session_id[:12]}",
        )

    async def _stop_handoff(self) -> None:
        task = self._handoff_task
        self._handoff_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def reset(self, *, strict: bool = False) -> None:
        await self._stop_handoff()
        await self._stop_context_resync()
        await self._stop_observer()
        async with self._event_lock:
            retire = self._opened and not self._retired
            envelope = self._envelope()
            self.activation_id = None
            self._notify_runtime_state()
        # whenIdle（远端结算）可能等待回传；释放事件锁，使旧回传能及时得到 stale（已过期）。
        if retire:
            try:
                response = await self.client.call("session.retire", envelope)
                if strict and response.get("result") not in {"accepted", "duplicate", "not_found"}:
                    raise DshBridgeError("DSH（后台代理）退役未获确认，保留交接事务。")
            except DshBridgeError:
                if strict:
                    raise
            await self._save_mapping(state="retired", retired=True)
        async with self._event_lock:
            self.dsh_session_id = f"voice-shadow-{uuid.uuid4()}"
            self._telemetry.clear()
            self._telemetry_trace_revision = -1
            self.epoch = int(self.context_source.shadow_context_epoch)
            self.last_applied_revision = -1
            self.last_guided_revision = 0
            self.last_claimed_revision = 0
            self._deferred_revision = 0
            self._gate_requested_revision = 0
            self._gate_generation += 1
            self.activation_id = None
            self._opened = False
            self._mapping_checked = True
            self._binding_restore_checked = True
            self._resume_existing = False
            self._retired = False
            self._waiting_user = False
            self._accepted_events.clear()
            self._accepted_requests.clear()
            self._last_terminal_activation_id = None
            self._last_terminal_dsh_session_id = None
            self._last_terminal_event_id = None
            self._last_terminal_event_digest = None
            self._observer_trace_revision = 0
            self._visible_status = ""

    async def cancel(self, *, strict: bool = False) -> None:
        await self._stop_handoff()
        await self._stop_context_resync()
        async with self._event_lock:
            cancel = self._opened and not self._retired
            envelope = self._envelope()
            self.activation_id = None
            self._waiting_user = False
            self._deferred_revision = 0
            self._gate_requested_revision = 0
            self.last_guided_revision = self.last_applied_revision
            self._gate_generation += 1
        if cancel:
            try:
                response = await self.client.call("session.cancel", envelope)
                if strict and response.get("result") not in {"accepted", "duplicate"}:
                    raise DshBridgeError("DSH（后台代理）取消未获确认，保留原会话。")
            except DshBridgeError:
                if strict:
                    raise
        await self._activity("cancelled")

    async def close(self) -> None:
        await self._stop_handoff()
        await self._stop_context_resync()
        await self._stop_observer()
        async with self._event_lock:
            suspend = self._opened and not self._retired
            envelope = self._envelope()
            self._retired = True
            self.activation_id = None
            self._notify_runtime_state()
        if suspend:
            try:
                await self.client.call("session.cancel", envelope)
            except DshBridgeError:
                pass
            # 关闭进程／重建组件仅暂停执行；退役由显式重置和一小时交接拥有。
            await self._save_mapping(state="waiting_user" if self._waiting_user else "idle")
        providers = [self.status_provider]
        if self.gate_provider is not self.status_provider:
            providers.append(self.gate_provider)
        for provider in providers:
            close = getattr(provider, "close", None)
            if callable(close):
                await close()

    async def accept_event(self, payload: dict[str, Any]) -> BridgeResult:
        async with self._event_lock:
            try:
                event_body = _strict_event(payload)
            except DshBridgeError:
                return "conflict"
            event_id = str(payload["event_id"])
            encoded = json.dumps(event_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            request_id = str(payload["request_id"])
            request_digest = hashlib.sha256(json.dumps(
                {"event_id": event_id, "event": event_body},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            event_type = str(event_body["type"])
            terminal = event_type in {"input_required", "result", "no_action", "failed"}
            if self._last_terminal_event_id == event_id:
                if self._last_terminal_event_digest != digest:
                    return "conflict"
                return "duplicate" if self.is_terminal_replay(
                    payload, normalized_event=event_body,
                ) else "stale"
            if not await self._callback_matches_or_reconcile(payload):
                return "stale"
            self._notify_runtime_state()
            previous_request = self._accepted_requests.get(request_id)
            if previous_request is not None:
                return "duplicate" if previous_request == request_digest else "conflict"
            previous = self._accepted_events.get(event_id)
            if previous is not None:
                return "duplicate" if previous == digest else "conflict"

            activation_id = str(self.activation_id or "")
            if event_type == "input_required":
                text = str(event_body["question"])
            elif event_type == "failed":
                text = str(event_body["message"])
            else:
                text = str(event_body.get("text") or event_body.get("reason") or "")
            event = ShadowEvent(
                event_id=event_id,
                kind=event_type,  # type: ignore[arg-type]
                text=text,
                based_on_context_revision=int(payload["context_revision"]),
                activation_id=activation_id,
                source="agent",
                options=tuple(event_body.get("options") or ()),
                failure_code=(str(event_body["code"]) if event_type == "failed" else None),
                infrastructure=bool(event_body.get("infrastructure", False)),
            )
            if self.production_sink is not None:
                await self.production_sink([event])
            self._accepted_events[event_id] = digest
            self._accepted_requests[request_id] = request_digest
            if len(self._accepted_requests) > 4096:
                self._accepted_requests.pop(next(iter(self._accepted_requests)))

            if terminal:
                if self._gate_enabled and max(
                    self._gate_requested_revision, self.last_applied_revision,
                ) > self.last_guided_revision:
                    self._deferred_revision = max(
                        self._deferred_revision, self._gate_requested_revision,
                        self.last_applied_revision,
                    )
                self._last_terminal_activation_id = activation_id
                self._last_terminal_dsh_session_id = self.dsh_session_id
                self._last_terminal_event_id = event_id
                self._last_terminal_event_digest = digest
                self._waiting_user = event_type == "input_required"
                self.activation_id = None
                state = "waiting_user" if self._waiting_user else (
                    "failed" if event_type == "failed" else "idle"
                )
                await self._activity(state, terminal=event_type)
                await self._save_mapping(state=state)
                if self._deferred_revision > 0:
                    self._schedule_handoff()
            else:
                await self._save_mapping(state="active")
            return "accepted"

    async def accept_early_silence(
        self, payload: dict[str, Any], request: Callable[[], Awaitable[str]],
    ) -> tuple[BridgeResult, str]:
        """Authorize one root activation's single-purpose silence request."""
        expected = {
            "protocol_version", "request_id", "voice_session_id", "dsh_session_id",
            "epoch", "context_revision", "activation_id",
        }
        if set(payload) != expected or payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
            return "conflict", "invalid_request"
        try:
            for field in ("request_id", "voice_session_id", "dsh_session_id", "activation_id"):
                if not isinstance(payload[field], str):
                    return "conflict", "invalid_request"
                _safe_identifier(payload[field], field=field)
            for field, minimum in (("epoch", 1), ("context_revision", 0)):
                value = payload[field]
                if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                    return "conflict", "invalid_request"
            request_id = payload["request_id"]
        except (KeyError, DshBridgeError):
            return "conflict", "invalid_request"
        async with self._event_lock:
            if self.activation_id is None or not await self._callback_matches_or_reconcile(payload):
                return "stale", "stale_binding"
            identity = f"{payload['activation_id']}:{payload['epoch']}"
            previous = self._accepted_early_silence.get(request_id)
            if previous is not None:
                return ("duplicate", previous[1]) if previous[0] == identity else ("conflict", "request_id_conflict")
            status = await request()
            if status in {"scheduled", "already_pending", "already_finishing"}:
                self._accepted_early_silence[request_id] = (identity, status)
                if len(self._accepted_early_silence) > 4096:
                    self._accepted_early_silence.pop(next(iter(self._accepted_early_silence)))
                return "accepted", status
            return "conflict", status

    async def accept_activity(self, payload: dict[str, Any]) -> BridgeResult:
        async with self._event_lock:
            # 用量属于绑定会话；终态工具结算后仍接收最终供应商计数，不改变激活状态。
            if (payload.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
                    and payload.get("voice_session_id") == self.voice_session_id
                    and payload.get("dsh_session_id") == self.dsh_session_id
                    and payload.get("epoch") == self.epoch
                    and payload.get("context_revision") == self.last_applied_revision
                    and not self._retired):
                self._update_telemetry(payload)
            if not await self._callback_matches_or_reconcile(payload):
                return "stale"
            claimed = payload.get("claimed_revision")
            if isinstance(claimed, int) and not isinstance(claimed, bool) and claimed >= self.last_claimed_revision:
                if claimed > self.last_claimed_revision:
                    self.last_claimed_revision = claimed
                    if self._gate_enabled:
                        log.info("dsh.context_gate claimed voice_session_id=%s activation_id=%s claimed_revision=%s",
                                 self.voice_session_id, self.activation_id, claimed)
                    await self._save_mapping(state="active")
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

    def _update_telemetry(self, payload: dict[str, Any]) -> None:
        """telemetry（插件用量）只允许有限的非负计数及缓存比例。"""
        revision = payload.get("trace_revision")
        value = payload.get("telemetry")
        if (not isinstance(value, dict) or type(revision) is not int
                or revision < 0 or revision < self._telemetry_trace_revision):
            return
        counts = ("context_token_estimate", "context_limit_tokens",
                  "provider_input_tokens", "provider_cached_tokens")
        safe: dict[str, int | float] = {}
        for key in counts:
            count = value.get(key)
            if type(count) is int and 0 <= count <= 2**53 - 1:
                safe[key] = count
        for key in ("context_usage_percent", "provider_cache_hit_ratio"):
            count = value.get(key)
            if (type(count) in (int, float) and math.isfinite(count) and count >= 0
                    and (key != "provider_cache_hit_ratio" or count <= 1)):
                safe[key] = count
        self._telemetry = safe
        self._telemetry_trace_revision = revision

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
            self._update_telemetry(response)
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
        progress（影子进度）另由二等信箱按 4 秒检查点评估，不走普通信息唤醒。
        """

        if self.production_sink is None:
            return
        normalized = text.strip()
        if not normalized or len(normalized) > 2000:
            log.warning(
                "dsh.status_observer_summary_rejected voice_session_id=%s chars=%s",
                self.voice_session_id, len(normalized),
            )
            return
        async with self._event_lock:
            activation_id = self.activation_id
            if activation_id is None:
                return
            await self.production_sink([ShadowEvent(
                event_id=uuid.uuid4().hex,
                kind="information",
                text=normalized,
                based_on_context_revision=self.last_applied_revision,
                activation_id=activation_id,
                source="observer",
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
            **self._telemetry,
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
            "context_gate": {
                "enabled": self._gate_enabled,
                "synced_revision": self.last_applied_revision,
                "queued_revision": self.last_guided_revision,
                "claimed_revision": self.last_claimed_revision,
                "deferred_revision": self._deferred_revision,
            },
            "state": "retired" if self._retired else ("active" if self.activation_id else "idle"),
        }

    def _callback_matches(self, payload: dict[str, Any]) -> bool:
        return (
            payload.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
            and payload.get("voice_session_id") == self.voice_session_id
            and payload.get("dsh_session_id") == self.dsh_session_id
            and payload.get("epoch") == self.epoch
            and self.epoch == int(self.context_source.shadow_context_epoch)
            and payload.get("activation_id") == self.activation_id
            and payload.get("context_revision") == self.last_applied_revision
            and not self._retired
        )

    async def _callback_matches_or_reconcile(self, payload: dict[str, Any]) -> bool:
        if self._callback_matches(payload):
            return True
        if (
            payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
            or payload.get("voice_session_id") != self.voice_session_id
            or payload.get("dsh_session_id") != self.dsh_session_id
            or payload.get("epoch") != self.epoch
            or self.epoch != int(self.context_source.shadow_context_epoch)
            or payload.get("activation_id") != self.activation_id
            or self._retired
        ):
            return False
        payload_revision = payload.get("context_revision")
        current_revision = int(self.context_source.shadow_input_revision)
        if (
            not isinstance(payload_revision, int)
            or isinstance(payload_revision, bool)
            or payload_revision <= self.last_applied_revision
            or payload_revision > current_revision
        ):
            return False
        try:
            state = await self._session_state()
        except DshBridgeError:
            return False
        state_revision = state.get("context_revision")
        if (
            not isinstance(state_revision, int)
            or isinstance(state_revision, bool)
            or state_revision < payload_revision
            or state_revision > current_revision
            or str(state.get("activation_id") or "") != str(self.activation_id or "")
        ):
            return False
        self.last_applied_revision = state_revision
        await self._save_mapping(state="active")
        log.info(
            "dsh.callback reconciled voice_session_id=%s activation_id=%s "
            "payload_revision=%s state_revision=%s",
            self.voice_session_id, self.activation_id,
            payload_revision, state_revision,
        )
        return True

    def is_terminal_replay(
        self, payload: dict[str, Any], *, normalized_event: dict[str, Any] | None = None,
    ) -> bool:
        """Recognize exactly the last persisted terminal callback after restart."""

        if not self.matches_last_terminal_identity(payload):
            return False
        try:
            event_body = normalized_event or _strict_event(payload)
        except DshBridgeError:
            return False
        if event_body.get("type") not in {"input_required", "result", "no_action", "failed"}:
            return False
        encoded = json.dumps(event_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest() == self._last_terminal_event_digest

    def matches_last_terminal_identity(self, payload: dict[str, Any]) -> bool:
        """Match receipt routing fields, leaving body conflicts to accept_event."""

        if (
            payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
            or payload.get("voice_session_id") != self.voice_session_id
            or payload.get("dsh_session_id") != self._last_terminal_dsh_session_id
            or payload.get("epoch") != self.epoch
            or self.epoch != int(self.context_source.shadow_context_epoch)
            or payload.get("activation_id") != self._last_terminal_activation_id
            or payload.get("event_id") != self._last_terminal_event_id
        ):
            return False
        return self.epoch == int(self.context_source.shadow_context_epoch)

    async def _stop_context_resync(self) -> None:
        task = self._context_resync_task
        self._context_resync_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _envelope(self, **extra: Any) -> dict[str, Any]:
        return {
            "voice_session_id": self.voice_session_id,
            "dsh_session_id": self.dsh_session_id,
            "epoch": self.epoch,
            **extra,
        }

    async def _save_mapping(self, *, state: str, retired: bool = False) -> None:
        now = time.time()
        await self.mapping_store.save(DshSessionMapping(
            schema_version=2,
            voice_session_id=self.voice_session_id,
            backend="dsh",
            dsh_session_id=self.dsh_session_id,
            epoch=self.epoch,
            state=state,
            last_applied_revision=self.last_applied_revision,
            last_activation_id=self.activation_id,
            last_terminal_activation_id=self._last_terminal_activation_id,
            last_terminal_dsh_session_id=self._last_terminal_dsh_session_id,
            last_terminal_event_id=self._last_terminal_event_id,
            last_terminal_event_digest=self._last_terminal_event_digest,
            gate_queued_revision=self.last_guided_revision,
            gate_claimed_revision=self.last_claimed_revision,
            gate_deferred_revision=self._deferred_revision,
            created_at=self._created_at,
            updated_at=now,
            retired_at=now if retired else None,
        ))

    def _notify_runtime_state(self) -> None:
        """同步 activation_id（有效激活标识），即使没有界面活动订阅也清理旧进度。"""
        setter = getattr(self.context_source, "set_shadow_runtime_state", None)
        if callable(setter):
            setter(available=self.available, active=self.activation_id is not None,
                   activation_id=self.activation_id or "")

    async def _activity(self, state: str, **extra: Any) -> None:
        self._notify_runtime_state()
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
