"""Device task routing（设备任务路由）：固定目标并保存未知执行结果。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from .registry import DeviceConnection, DeviceRegistry
from .task_journal import DeviceTaskJournal, TaskConflict, TaskRecord


def _snapshot_json(value: Any) -> Any:
    """_snapshot_json（冻结请求内容）：台账与设备执行必须使用同一份数据。"""
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("设备消息超过允许的大小。")
        return json.loads(encoded)
    except (TypeError, RecursionError) as exc:
        raise ValueError("设备消息不是有效 JSON（结构化数据）。") from exc


class DeviceUnavailable(RuntimeError):
    """DeviceUnavailable（设备不可用）：请求尚未受理或等待原任务结果。"""


class DeviceTargetChanged(DeviceUnavailable):
    """DeviceTargetChanged（执行目标代次已变化）：本次请求尚未派发。"""


class DeviceOutcomeUnknown(RuntimeError):
    """DeviceOutcomeUnknown（设备执行结果未知）：不能自动重做。"""


@dataclass(frozen=True)
class DeviceRequest:
    """DeviceRequest（设备请求）：每个动作绑定固定设备与连接代次。"""

    request_id: str  # request_id（控制请求编号）。
    device_id: str  # device_id（固定设备编号）。
    core_session_id: str  # core_session_id（权威核心运行编号）。
    task_id: str  # task_id（持久设备任务编号，本路由与请求编号一致）。
    connection_generation: int  # connection_generation（设备连接代次）。
    executor_generation: int  # executor_generation（设备执行器代次）。
    operation: str  # operation（操作编号）。
    arguments: Any  # arguments（已冻结的 JSON 参数）。


@dataclass(frozen=True)
class DeviceResponse:
    """DeviceResponse（设备回执）：必须对应原请求与原设备执行器。"""

    request_id: str  # request_id（对应的控制请求编号）。
    device_id: str  # device_id（实际执行设备编号）。
    core_session_id: str  # core_session_id（对应的权威核心运行编号）。
    task_id: str  # task_id（对应的持久设备任务编号）。
    connection_generation: int  # connection_generation（原连接代次）。
    executor_generation: int  # executor_generation（原执行器代次）。
    succeeded: bool  # succeeded（设备操作是否成功）。
    result: Any  # result（纯 JSON 操作结果）。


class DeviceTransport(Protocol):
    """DeviceTransport（设备传输）：由已认证的设备连接实现。"""

    async def execute(self, request: DeviceRequest) -> DeviceResponse: ...


class DeviceRouter:
    """DeviceRouter（设备路由）：调用者负责认证和动作授权。"""

    def __init__(self, registry: DeviceRegistry, journal: DeviceTaskJournal, *, run_id: str | None = None) -> None:
        self.registry = registry
        self.journal = journal
        self.run_id = run_id or uuid4().hex
        self._connections: dict[str, tuple[DeviceConnection, DeviceTransport]] = {}
        self._task_queryers: dict[str, Callable[[str], Awaitable[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    def register_task_queryer(
        self, device_id: str, queryer: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        """register_task_queryer（绑定只读回执查询）：仅注册查询函数，不派发设备动作。"""
        if not isinstance(device_id, str) or not device_id or device_id != device_id.strip():
            raise ValueError("原任务查询设备编号无效。")
        if not callable(queryer):
            raise TypeError("原任务查询接口必须可调用。")
        self._task_queryers[device_id] = queryer

    async def connect(self, device_id: str, transport: DeviceTransport) -> DeviceConnection:
        """connect（登记连接）：只能由已经验证设备身份的调用者使用。"""
        async with self._lock:
            connection = self.registry.connect(device_id)
            self._connections[device_id] = (connection, transport)
            return connection

    async def attach_transport(self, device_id: str, transport: DeviceTransport) -> DeviceConnection:
        """attach_transport（绑定已登记传输）：不改变连接租约或当前交互设备。"""
        # Protocol（协议）未声明 runtime_checkable；检查运行时所需的公开方法。
        if not callable(getattr(transport, "execute", None)):
            raise TypeError("设备传输缺少 execute（执行）接口。")
        async with self._lock:
            connection = self.registry.connection(device_id)
            if connection is None or not connection.connected:
                raise DeviceUnavailable("目标设备未登记或未连接。")
            self._connections[device_id] = (connection, transport)
            return connection

    async def has_transport(self, device_id: str) -> bool:
        """has_transport（查询已认证执行通道）：不创建、续租或切换设备连接。"""
        async with self._lock:
            entry = self._connections.get(device_id)
            if entry is None:
                return False
            connection, transport = entry
            is_connected = getattr(transport, "is_connected", None)
            return (connection.connected
                    and self.registry.connection(device_id) == connection
                    and (not callable(is_connected) or is_connected() is True))

    async def disconnect(self, device_id: str, connection_generation: int) -> bool:
        """disconnect（断开连接）：旧连接不能清除已替换的连接。"""
        async with self._lock:
            if not self.registry.disconnect(device_id, connection_generation):
                return False
            current = self._connections.get(device_id)
            if current is not None and current[0].connection_generation == connection_generation:
                self._connections.pop(device_id, None)
            return True

    async def restart_executor(self, device_id: str, connection_generation: int,
                               transport: DeviceTransport) -> DeviceConnection:
        """restart_executor（重建执行器）：保留连接，替换执行器并隔离旧回执。"""
        async with self._lock:
            current = self._connections.get(device_id)
            if current is None or current[0].connection_generation != connection_generation:
                raise DeviceUnavailable("目标设备连接已失效。")
            if self.registry.connection(device_id) != current[0]:
                raise DeviceUnavailable("设备连接或执行器状态已变化。")
            connection = self.registry.restart_executor(device_id, connection_generation)
            self._connections[device_id] = (connection, transport)
            return connection

    async def execute(self, *, request_id: str, device_id: str, operation: str,
                      arguments: Any, timeout_seconds: float = 30.0,
                      expected_connection: DeviceConnection | None = None) -> TaskRecord:
        """execute（执行）：按指定连接快照受理并发送；重复请求只查询既有记录。"""
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 600:
            raise ValueError("设备操作超时必须大于零且不超过 600 秒。")
        arguments = _snapshot_json(arguments)
        # 已受理的请求在设备断线时仍可查询；begin 同时核对原目标和参数。
        existing = await asyncio.to_thread(self.journal.get, request_id)
        if existing is not None:
            record, _ = await asyncio.to_thread(
                self.journal.begin, request_id=request_id, device_id=device_id, operation=operation,
                arguments=arguments, run_id=self.run_id,
            )
            return record
        async with self._lock:
            entry = self._connections.get(device_id)
            if expected_connection is not None and (
                entry is None or entry[0] != expected_connection
                or self.registry.connection(device_id) != expected_connection
            ):
                raise DeviceTargetChanged("固定目标设备的连接或执行器代次已变化。")
            if entry is None:
                raise DeviceUnavailable("目标设备未连接。")
            connection, transport = entry
        begin_task = asyncio.create_task(asyncio.to_thread(
            self.journal.begin, request_id=request_id, device_id=device_id, operation=operation,
            arguments=arguments, run_id=self.run_id,
        ))
        try:
            record, created = await asyncio.shield(begin_task)
        except asyncio.CancelledError:
            # 数据库线程不能被取消；先确认是否受理，避免留下永远无法结算的已受理任务。
            try:
                _, created = await asyncio.shield(begin_task)
                if created:
                    await asyncio.shield(asyncio.to_thread(self.journal.mark_unknown, request_id, self.run_id))
            finally:
                raise
        if not created:
            return record
        start_task = asyncio.create_task(asyncio.to_thread(self.journal.start, request_id, self.run_id))
        try:
            started = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(start_task)
                await asyncio.shield(asyncio.to_thread(self.journal.mark_unknown, request_id, self.run_id))
            finally:
                raise
        except Exception as exc:
            try:
                await asyncio.to_thread(self.journal.mark_unknown, request_id, self.run_id)
            except Exception:
                pass  # 台账不可用时不假定任务尚未执行；调用方必须查询原请求。
            raise DeviceOutcomeUnknown("任务执行状态写入失败，请查询原请求。") from exc
        if not started:
            raise DeviceOutcomeUnknown("任务无法确认执行状态，请查询原请求。")
        request = DeviceRequest(request_id, device_id, self.run_id, request_id,
                                connection.connection_generation, connection.executor_generation,
                                operation, arguments)
        try:
            async with self._lock:
                current = self._connections.get(device_id)
                if (current is None or current[0] != connection
                        or self.registry.connection(device_id) != connection):
                    raise DeviceOutcomeUnknown("设备连接已变化，任务结果未知。")
            response = await asyncio.wait_for(transport.execute(request), timeout=float(timeout_seconds))
            async with self._lock:
                current = self._connections.get(device_id)
                if (current is None or current[0] != connection
                        or self.registry.connection(device_id) != connection):
                    raise DeviceOutcomeUnknown("设备连接已变化，不能确认旧回执。")
            if (not isinstance(response, DeviceResponse) or type(response.succeeded) is not bool
                    or response.request_id != request_id or response.device_id != device_id
                    or response.core_session_id != self.run_id or response.task_id != request_id
                    or response.connection_generation != connection.connection_generation
                    or response.executor_generation != connection.executor_generation):
                raise DeviceOutcomeUnknown("设备回执与原任务或设备代次不匹配。")
            result = _snapshot_json(response.result)
            if not await asyncio.to_thread(self.journal.finish, request_id, self.run_id,
                                           succeeded=response.succeeded, result=result):
                raise DeviceOutcomeUnknown("任务结果未能写入原记录。")
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(self.journal.mark_unknown, request_id, self.run_id))
            raise
        except Exception as exc:
            await asyncio.to_thread(self.journal.mark_unknown, request_id, self.run_id)
            if isinstance(exc, DeviceOutcomeUnknown):
                raise
            raise DeviceOutcomeUnknown("设备执行结果未确认，请查询原任务后再操作。") from exc
        settled = await asyncio.to_thread(self.journal.get, request_id)
        if settled is None:
            raise DeviceOutcomeUnknown("任务结算记录不可用。")
        return settled

    async def query(self, *, request_id: str, device_id: str) -> TaskRecord | None:
        """query（只读原任务查询）：读台账及已缓存设备回执，不重派原操作。"""
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 128
                or request_id != request_id.strip()):
            raise ValueError("请求编号无效。")
        if (not isinstance(device_id, str) or not device_id or len(device_id) > 128
                or device_id != device_id.strip()):
            raise ValueError("设备编号无效。")
        record = await asyncio.to_thread(self.journal.get, request_id)
        if record is not None and record.device_id != device_id:
            raise TaskConflict("原请求编号属于其他设备，拒绝跨设备查询。")
        if record is not None and record.status == "unknown":
            async with self._lock:
                entry = self._connections.get(device_id)
                transport = entry[1] if entry is not None else None
                registered_queryer = self._task_queryers.get(device_id)
            query_completed = registered_queryer or getattr(transport, "query_completed", None)
            if callable(query_completed):
                try:
                    remote = await query_completed(request_id)
                    if (isinstance(remote, dict) and remote.get("status") == "completed"
                            and type(remote.get("succeeded")) is bool):
                        result = _snapshot_json(remote.get("result"))
                        resolved = await asyncio.to_thread(
                            self.journal.resolve_unknown, request_id, device_id=device_id,
                            request_digest=record.request_digest,
                            succeeded=remote["succeeded"], result=result,
                        )
                        if resolved:
                            record = await asyncio.to_thread(self.journal.get, request_id)
                        else:
                            record = await asyncio.to_thread(self.journal.get, request_id)
                except Exception:
                    # 原设备没有可查询回执、连接已变或台账结算失败时仍保留 unknown。
                    record = await asyncio.to_thread(self.journal.get, request_id)
        return record

    async def resolve_unknown_from_receipt(
        self, *, request_id: str, device_id: str, succeeded: bool, result: Any,
    ) -> TaskRecord | None:
        """resolve_unknown_from_receipt（提交原设备晚到回执）：匹配设备及原请求摘要后更新未知行。"""
        if type(succeeded) is not bool:
            raise ValueError("原设备回执状态必须为布尔值。")
        result = _snapshot_json(result)
        for attempt in range(20):
            record = await asyncio.to_thread(self.journal.get, request_id)
            if record is None:
                return None
            if record.device_id != device_id:
                raise TaskConflict("晚到设备回执目标与原任务不匹配。")
            if record.status == "unknown":
                await asyncio.to_thread(
                    self.journal.resolve_unknown, request_id, device_id=device_id,
                    request_digest=record.request_digest, succeeded=succeeded, result=result,
                )
                return await asyncio.to_thread(self.journal.get, request_id)
            if record.status in {"succeeded", "failed"}:
                return record
            # The original router waiter may still be committing its result or timeout state.
            if attempt < 19:
                await asyncio.sleep(0.005)
        return await asyncio.to_thread(self.journal.get, request_id)
