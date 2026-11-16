"""In-memory device leases（内存中的设备连接与交互租约）。

此模块不执行认证、保存配置或操作设备。调用方必须先完成身份及能力检查。
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class DeviceConnection:
    """DeviceConnection（设备连接）：连接或执行器重建后代次递增。"""

    device_id: str
    connection_generation: int
    executor_generation: int
    connected: bool
    capabilities: tuple[str, ...] = ()  # capabilities（已认证设备能力编号）。


@dataclass(frozen=True)
class InteractionLease:
    """InteractionLease（交互租约）：只允许一个已连接设备提交主语音。"""

    device_id: str
    interaction_generation: int
    connection_generation: int
    executor_generation: int


class DeviceRegistry:
    """DeviceRegistry（设备注册表）：进程内串行化设备连接和手动切换。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._devices: dict[str, DeviceConnection] = {}
        self._interaction_generation = 0
        self._active: InteractionLease | None = None
        self._pending: InteractionLease | None = None

    @staticmethod
    def _validate_id(device_id: str) -> None:
        if not isinstance(device_id, str) or not device_id or len(device_id) > 128 or device_id != device_id.strip():
            raise ValueError("设备编号必须为 1 至 128 个非空字符，首尾不能有空白。")

    def connect(self, device_id: str, *, capabilities: tuple[str, ...] = ()) -> DeviceConnection:
        """connect（连接）：重连撤销旧代次的交互授权，不自动接管。"""
        self._validate_id(device_id)
        if (not isinstance(capabilities, tuple) or len(capabilities) > 32
                or any(not isinstance(value, str) or not value or len(value) > 64
                       or value != value.strip() or not value.isascii()
                       or not value[0].islower()
                       or any(not (char.islower() or char.isdigit() or char == "_") for char in value)
                       for value in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise ValueError("设备能力必须为不重复的有限编号元组。")
        with self._lock:
            previous = self._devices.get(device_id)
            connection = DeviceConnection(device_id, previous.connection_generation + 1 if previous else 1,
                                          previous.executor_generation + 1 if previous else 1, True,
                                          tuple(sorted(capabilities)))
            self._devices[device_id] = connection
            self._invalidate_device(device_id)
            return connection

    def disconnect(self, device_id: str, connection_generation: int) -> bool:
        """disconnect（断开）：迟到的旧连接关闭不能撤销新连接。"""
        with self._lock:
            current = self._devices.get(device_id)
            if current is None or not current.connected or current.connection_generation != connection_generation:
                return False
            self._devices[device_id] = DeviceConnection(device_id, current.connection_generation,
                                                        current.executor_generation, False, current.capabilities)
            self._invalidate_device(device_id)
            return True

    def restart_executor(self, device_id: str, connection_generation: int) -> DeviceConnection:
        """restart_executor（重建执行器）：旧观察和语音租约均失效。"""
        with self._lock:
            current = self._devices.get(device_id)
            if current is None or not current.connected or current.connection_generation != connection_generation:
                raise ValueError("设备连接已失效。")
            current = DeviceConnection(device_id, current.connection_generation, current.executor_generation + 1,
                                       True, current.capabilities)
            self._devices[device_id] = current
            self._invalidate_device(device_id)
            return current

    def _invalidate_device(self, device_id: str) -> None:
        if ((self._active is not None and self._active.device_id == device_id)
                or (self._pending is not None and self._pending.device_id == device_id)):
            self._interaction_generation += 1
            if self._active is not None and self._active.device_id == device_id:
                self._active = None
            if self._pending is not None and self._pending.device_id == device_id:
                self._pending = None

    @property
    def active(self) -> InteractionLease | None:
        with self._lock:
            return self._active

    @property
    def pending(self) -> InteractionLease | None:
        """pending（等待目标设备就绪的切换租约）：尚未授予语音输入。"""
        with self._lock:
            return self._pending

    def connection(self, device_id: str) -> DeviceConnection | None:
        """connection（连接快照）：供已授权的设备路由核对当前代次。"""
        with self._lock:
            return self._devices.get(device_id)

    def snapshot(self) -> tuple[DeviceConnection, ...]:
        """snapshot（设备快照）：返回不可变连接信息，不暴露连接凭据。"""
        with self._lock:
            return tuple(self._devices[key] for key in sorted(self._devices))

    def snapshot_with_active(self) -> tuple[tuple[DeviceConnection, ...], InteractionLease | None]:
        """snapshot_with_active（设备与占用快照）：避免切换中读取互相矛盾的状态。"""
        with self._lock:
            return tuple(self._devices[key] for key in sorted(self._devices)), self._active

    def state_snapshot(self) -> tuple[tuple[DeviceConnection, ...], int, InteractionLease | None]:
        """state_snapshot（完整设备运行快照）：同一锁内读取设备、交互代次与活跃租约。"""
        with self._lock:
            return tuple(self._devices[key] for key in sorted(self._devices)), self._interaction_generation, self._active

    def interaction_snapshot(self) -> tuple[int, InteractionLease | None]:
        """interaction_snapshot（交互快照）：代次与占用属于同一时刻。"""
        with self._lock:
            return self._interaction_generation, self._active

    @property
    def generation(self) -> int:
        with self._lock:
            return self._interaction_generation

    def prepare_switch(self, device_id: str, expected_generation: int) -> InteractionLease:
        """prepare_switch（准备切换）：撤销旧输入，等待调用方结算与设备就绪。"""
        with self._lock:
            if expected_generation != self._interaction_generation:
                raise ValueError("交互设备状态已变化，请重新选择。")
            if self._pending is not None:
                raise ValueError("另一个交互设备切换尚未完成。")
            target = self._devices.get(device_id)
            if target is None or not target.connected:
                raise ValueError("目标设备未连接。")
            self._interaction_generation += 1
            self._active = None
            self._pending = InteractionLease(device_id, self._interaction_generation,
                                             target.connection_generation, target.executor_generation)
            return self._pending

    def commit_switch(self, lease: InteractionLease) -> bool:
        """commit_switch（确认切换）：只接受当前准备事务和原设备代次。"""
        with self._lock:
            if self._pending != lease:
                return False
            current = self._devices.get(lease.device_id)
            if (current is None or not current.connected
                    or current.connection_generation != lease.connection_generation
                    or current.executor_generation != lease.executor_generation):
                return False
            self._active, self._pending = lease, None
            return True

    def abort_switch(self, lease: InteractionLease) -> bool:
        """abort_switch（取消切换）：不自动恢复已撤销的旧占用权。"""
        with self._lock:
            if self._pending != lease:
                return False
            self._pending = None
            self._interaction_generation += 1
            return True

    def accepts(self, lease: InteractionLease) -> bool:
        """accepts（接纳输入）：只接受当前交互租约。"""
        with self._lock:
            return self._active is not None and self._active == lease
