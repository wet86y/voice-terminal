"""Remote device task session（远端设备任务会话）：只经已认证客户端连接派发任务。"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .router import (
    DeviceOutcomeUnknown, DeviceRequest, DeviceResponse, DeviceRouter, DeviceUnavailable, _snapshot_json,
)
from .registry import DeviceConnection

REMOTE_DESKTOP_CAPABILITY = "desktop_control"  # 远端桌面控制能力编号。
REMOTE_CAPABILITIES = frozenset({"audio_stream", "client_tts", "desktop_control", "file_transfer", "screen_capture", "terminal_control"})  # REMOTE_CAPABILITIES（远端受管能力清单）。
MAX_REMOTE_SESSION_TASKS = 32  # 单个设备会话有界排队任务数。
MAX_REMOTE_SESSION_PENDING = 64  # 单个设备会话最大在途请求数。
REMOTE_SESSION_IDLE_SECONDS = 90.0  # 最后一次客户端回执或轮询后的空闲失效时限。


@dataclass(frozen=True)
class RemoteDeviceTask:
    """RemoteDeviceTask（远端设备任务）：固定目标及所有相关代次。"""

    request_id: str  # request_id（控制请求编号）。
    device_id: str  # device_id（固定执行设备编号）。
    core_session_id: str  # core_session_id（权威核心运行编号）。
    task_id: str  # task_id（持久任务编号）。
    connection_generation: int  # connection_generation（设备连接代次）。
    executor_generation: int  # executor_generation（设备执行器代次）。
    sequence: int  # sequence（设备连接内严格递增任务序号）。
    operation: str  # operation（设备操作编号）。
    arguments: Any  # arguments（已冻结的 JSON 参数）。

    def payload(self) -> dict[str, Any]:
        """payload（线缆任务）：提供原生客户端所需的目标与代次字段。"""
        return {
            "request_id": self.request_id,
            "device_id": self.device_id,
            "core_session_id": self.core_session_id,
            "task_id": self.task_id,
            "connection_generation": self.connection_generation,
            "executor_generation": self.executor_generation,
            "sequence": self.sequence,
            "operation": self.operation,
            "arguments": self.arguments,
            "fields": {
                "request_id": "核心控制请求编号",
                "device_id": "固定执行设备编号",
                "core_session_id": "权威核心运行编号",
                "task_id": "持久设备任务编号",
                "connection_generation": "设备连接代次",
                "executor_generation": "设备执行器代次",
                "sequence": "本次连接内的任务序号",
                "operation": "桌面设备操作编号",
                "arguments": "经大小及 JSON 校验的桌面操作参数",
            },
        }


@dataclass
class _PendingRemoteTask:
    task: RemoteDeviceTask
    future: asyncio.Future[DeviceResponse]
    claimed: bool = False  # claimed（设备客户端已领取；超时后仍可查询晚到回执）。


@dataclass
class _RemoteSession:
    device_id: str
    connection: DeviceConnection
    ready: asyncio.Condition
    last_seen: float
    sequence: int = 0
    closed: bool = False
    queue: list[RemoteDeviceTask] = field(default_factory=list)
    pending: dict[str, _PendingRemoteTask] = field(default_factory=dict)
    completed: OrderedDict[str, tuple[int, bool, Any]] = field(default_factory=OrderedDict)


class RemoteDeviceSessionHub:
    """RemoteDeviceSessionHub（远端设备会话管理）：连接失效时将原任务结算为未知，不重发。"""

    def __init__(self, router: DeviceRouter) -> None:
        self.router = router
        self._sessions: dict[str, _RemoteSession] = {}
        self._completed: dict[str, OrderedDict[str, tuple[int, int, int, bool, Any]]] = {}
        self._lock = asyncio.Lock()

    async def open(self, device_id: str, capabilities: list[str]) -> DeviceConnection:
        """open（登记远端客户端）：只有通过上游身份认证的网关路由可调用。"""
        if not isinstance(device_id, str) or not device_id or device_id == "local" or device_id != device_id.strip():
            raise ValueError("远端设备编号无效。")
        if (not isinstance(capabilities, list) or not capabilities or len(capabilities) > 16
                or any(not isinstance(value, str) for value in capabilities)
                or len(set(capabilities)) != len(capabilities)
                or any(value not in REMOTE_CAPABILITIES for value in capabilities)):
            raise ValueError("远端设备能力不在固定受管能力清单中。")
        async with self._lock:
            previous = self._sessions.get(device_id)
            if previous is not None:
                await self._retire_locked(previous, "远端设备连接已被新连接替换。")
            connection = self.router.registry.connect(device_id, capabilities=tuple(capabilities))
            session = _RemoteSession(
                device_id=device_id,
                connection=connection,
                ready=asyncio.Condition(self._lock),
                last_seen=time.monotonic(),
            )
            transport = _RemoteDeviceTransport(self, device_id, connection)
            try:
                await self.router.attach_transport(device_id, transport)
            except Exception:
                self.router.registry.disconnect(device_id, connection.connection_generation)
                raise
            self._sessions[device_id] = session
            self.router.register_task_queryer(device_id, lambda task_id, device=device_id: self.query_completed(device, task_id))
            return connection

    async def close(self, device_id: str, connection_generation: int) -> bool:
        """close（断开远端连接）：只允许对应代次关闭连接。"""
        async with self._lock:
            session = self._sessions.get(device_id)
            if session is None or session.connection.connection_generation != connection_generation:
                return False
            await self._retire_locked(session, "远端客户端已断开，任务结果未知。")
            self._sessions.pop(device_id, None)
            await self.router.disconnect(device_id, connection_generation)
            return True

    async def close_all(self) -> None:
        """close_all（核心退出清理）：关闭所有会话并让在途任务保持未知。"""
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                await self._retire_locked(session, "权威核心已关闭，远端任务结果未知。")
                await self.router.disconnect(session.device_id, session.connection.connection_generation)

    def is_connected(self, device_id: str, connection_generation: int, executor_generation: int) -> bool:
        """is_connected（检查会话租约）：空闲、重连或旧代次均视为断开。"""
        session = self._sessions.get(device_id)
        return bool(session is not None and not session.closed
                    and session.connection.connection_generation == connection_generation
                    and session.connection.executor_generation == executor_generation
                    and self.router.registry.connection(device_id) == session.connection
                    and time.monotonic() - session.last_seen < REMOTE_SESSION_IDLE_SECONDS)

    async def poll(self, device_id: str, connection_generation: int, executor_generation: int,
                   wait_ms: int = 1000, *, lane: str | None = None) -> RemoteDeviceTask | None:
        """poll（轮询任务）：分离实时播报控制和普通设备任务，保留各自的领取顺序。"""
        if type(wait_ms) is not int or not 0 <= wait_ms <= 5000:
            raise ValueError("设备任务轮询等待范围必须为 0 至 5000 毫秒。")
        if lane not in (None, "general", "playback"):
            raise ValueError("设备任务轮询类别无效。")
        session = self._get_session(device_id, connection_generation, executor_generation)
        async with session.ready:
            session.last_seen = time.monotonic()
            deadline = session.last_seen + wait_ms / 1000
            while not session.closed:
                for index, task in enumerate(session.queue):
                    if lane is None or task.operation.startswith("playback.") == (lane == "playback"):
                        session.pending[task.task_id].claimed = True
                        session.last_seen = time.monotonic()
                        return session.queue.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(session.ready.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
            session.last_seen = time.monotonic()
            return None

    async def heartbeat(self, device_id: str, connection_generation: int,
                        executor_generation: int) -> str:
        """heartbeat（续接活跃任务租约）：不领取任务，只为进行中的长文件传输续租。"""
        async with self._lock:
            session = self._get_session(device_id, connection_generation, executor_generation)
            session.last_seen = time.monotonic()
            return "accepted"

    async def complete(self, *, device_id: str, connection_generation: int, executor_generation: int,
                       task_id: str, sequence: int, core_session_id: str, succeeded: bool,
                       result: Any) -> str:
        """complete（完成设备任务）：只接纳匹配原任务和原设备代次的回执。"""
        if (not isinstance(task_id, str) or not task_id or type(sequence) is not int or sequence < 1
                or not isinstance(core_session_id, str) or type(succeeded) is not bool):
            raise ValueError("设备任务回执字段无效。")
        result = _snapshot_json(result)
        session = self._get_session(device_id, connection_generation, executor_generation)
        async with self._lock:
            if self._sessions.get(device_id) is not session or session.closed:
                raise DeviceUnavailable("设备会话已变化，迟到回执不予结算。")
            pending = session.pending.get(task_id)
            if pending is None:
                previous = session.completed.get(task_id)
                if previous == (sequence, succeeded, result):
                    session.last_seen = time.monotonic()
                    return "already_completed"
                raise DeviceUnavailable("设备回执不属于当前在途任务。")
            task = pending.task
            if (not pending.claimed or task.sequence != sequence or task.core_session_id != core_session_id
                    or task.connection_generation != connection_generation
                    or task.executor_generation != executor_generation or task.device_id != device_id):
                raise DeviceUnavailable("设备回执与原任务或设备代次不匹配。")
            response = DeviceResponse(
                request_id=task.request_id,
                device_id=task.device_id,
                core_session_id=task.core_session_id,
                task_id=task.task_id,
                connection_generation=task.connection_generation,
                executor_generation=task.executor_generation,
                succeeded=succeeded,
                result=result,
            )
            if pending.future.done():
                previous = session.completed.get(task_id)
                if previous == (sequence, succeeded, result):
                    return "already_completed"
                raise DeviceUnavailable("原任务已结算，拒绝冲突回执。")
            pending.future.set_result(response)
            session.pending.pop(task_id, None)
            session.completed[task_id] = (sequence, succeeded, result)
            session.completed.move_to_end(task_id)
            while len(session.completed) > 128:
                session.completed.popitem(last=False)
            completed = self._completed.setdefault(device_id, OrderedDict())
            completed[task_id] = (
                connection_generation, executor_generation, sequence, succeeded, result,
            )
            completed.move_to_end(task_id)
            while len(completed) > 256:
                completed.popitem(last=False)
            session.last_seen = time.monotonic()
            original_request_id = task.request_id

        # A timed-out router caller no longer awaits this future. Persist its
        # late receipt immediately so a process restart does not erase a known result.
        await self.router.resolve_unknown_from_receipt(
            request_id=original_request_id, device_id=device_id,
            succeeded=succeeded, result=result,
        )
        return "accepted"

    async def query_completed(self, device_id: str, task_id: str) -> dict[str, Any]:
        """query_completed（只读原任务查询）：只返回已保存回执，不派发或重放操作。"""
        if (not isinstance(device_id, str) or not device_id or device_id != device_id.strip()
                or not isinstance(task_id, str) or not task_id or len(task_id) > 128
                or task_id != task_id.strip()):
            raise ValueError("远端原任务查询编号无效。")
        async with self._lock:
            completed = self._completed.get(device_id, {}).get(task_id)
            if completed is not None:
                connection_generation, executor_generation, sequence, succeeded, result = completed
                return {
                    "status": "completed", "connection_generation": connection_generation,
                    "executor_generation": executor_generation, "sequence": sequence,
                    "succeeded": succeeded, "result": result,
                }
            session = self._sessions.get(device_id)
            pending = session.pending.get(task_id) if session is not None and not session.closed else None
            if pending is not None:
                return {
                    "status": "running" if pending.claimed else "queued",
                    "connection_generation": session.connection.connection_generation,
                    "executor_generation": session.connection.executor_generation,
                }
            return {"status": "unknown"}

    async def _dispatch(self, request: DeviceRequest) -> DeviceResponse:
        session = self._get_session(
            request.device_id, request.connection_generation, request.executor_generation,
        )
        async with self._lock:
            if self._sessions.get(request.device_id) is not session or session.closed:
                raise DeviceUnavailable("设备会话已断开。")
            if len(session.pending) >= MAX_REMOTE_SESSION_PENDING:
                raise DeviceUnavailable("远端设备在途任务已达上限。")
            if len(session.queue) >= MAX_REMOTE_SESSION_TASKS:
                raise DeviceUnavailable("远端设备任务队列已满。")
            if request.task_id in session.pending:
                raise DeviceUnavailable("远端设备任务编号正在使用。")
            session.sequence += 1
            task = RemoteDeviceTask(
                request_id=request.request_id,
                device_id=request.device_id,
                core_session_id=request.core_session_id,
                task_id=request.task_id,
                connection_generation=request.connection_generation,
                executor_generation=request.executor_generation,
                sequence=session.sequence,
                operation=request.operation,
                arguments=_snapshot_json(request.arguments),
            )
            try:
                # This small bounded SQLite write must finish before the task becomes visible;
                # offloading it here can consume the caller's short admission timeout.
                self.router.journal.bind_remote_identity(
                    request_id=task.task_id,
                    device_id=task.device_id,
                    run_id=task.core_session_id,
                    connection_generation=task.connection_generation,
                    executor_generation=task.executor_generation,
                    sequence=task.sequence,
                )
            except Exception as exc:
                raise DeviceUnavailable("远端任务代次未能持久绑定；任务未进入设备队列。") from exc
            future: asyncio.Future[DeviceResponse] = asyncio.get_running_loop().create_future()
            session.queue.append(task)
            session.pending[task.task_id] = _PendingRemoteTask(task, future)
            session.last_seen = time.monotonic()
            session.ready.notify_all()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            async with self._lock:
                current = session.pending.get(task.task_id)
                if current is not None and current.future is future:
                    if current.claimed:
                        # The remote side may have performed a side effect. Keep the original
                        # request ID queryable until its late receipt or the session retires.
                        future.add_done_callback(_consume_future_exception)
                    else:
                        future.cancel()
                        session.pending.pop(task.task_id, None)
                        session.queue.remove(current.task)
            raise
        finally:
            async with self._lock:
                current = session.pending.get(task.task_id)
                if current is not None and current.future is future and future.done():
                    session.pending.pop(task.task_id, None)

    def _get_session(self, device_id: str, connection_generation: int,
                     executor_generation: int) -> _RemoteSession:
        session = self._sessions.get(device_id)
        if (session is None or session.closed
                or session.connection.connection_generation != connection_generation
                or session.connection.executor_generation != executor_generation
                or self.router.registry.connection(device_id) != session.connection
                or time.monotonic() - session.last_seen >= REMOTE_SESSION_IDLE_SECONDS):
            raise DeviceUnavailable("远端设备未连接或会话代次已失效。")
        return session

    async def _retire_locked(self, session: _RemoteSession, message: str) -> None:
        session.closed = True
        session.queue.clear()
        for pending in tuple(session.pending.values()):
            if not pending.future.done():
                pending.future.set_exception(DeviceOutcomeUnknown(message))
        session.pending.clear()
        session.ready.notify_all()


class _RemoteDeviceTransport:
    """_RemoteDeviceTransport（统一路由适配）：无任务回执时不重放。"""

    def __init__(self, hub: RemoteDeviceSessionHub, device_id: str,
                 connection: DeviceConnection) -> None:
        self.hub = hub
        self.device_id = device_id
        self.connection = connection

    def is_connected(self) -> bool:
        return self.hub.is_connected(self.device_id, self.connection.connection_generation,
                                     self.connection.executor_generation)

    async def execute(self, request: DeviceRequest) -> DeviceResponse:
        if (request.device_id != self.device_id
                or request.connection_generation != self.connection.connection_generation
                or request.executor_generation != self.connection.executor_generation):
            raise DeviceOutcomeUnknown("设备任务与远端会话代次不匹配。")
        return await self.hub._dispatch(request)

    async def query_completed(self, request_id: str) -> dict[str, Any]:
        """query_completed（查询同设备原请求）：只读取会话回执缓存。"""
        return await self.hub.query_completed(self.device_id, request_id)


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """_consume_future_exception（读取已无人等待的错误）：避免超时后未取异常告警。"""
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:
        pass
