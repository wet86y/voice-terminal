"""Screen routing（屏幕捕获路由）：主语音与代理共享固定的设备目标。"""
from __future__ import annotations

from threading import RLock
from typing import Any
from collections import OrderedDict

from .registry import DeviceRegistry


class ScreenCaptureRouter:
    """ScreenCaptureRouter（屏幕路由）：当前仅提供本机，绝不将远端目标回退到本机。"""

    def __init__(self, local: Any, registry: DeviceRegistry) -> None:
        self._local = local
        self._registry = registry
        self._lock = RLock()
        self._targets: OrderedDict[tuple[str | None, str | None], tuple] = OrderedDict()

    def __getattr__(self, name: str) -> Any:
        # 配置、来源租约和消费规则继续由既有截图服务维护。
        return getattr(self._local, name)

    @property
    def available(self) -> bool:
        """available（目标能力）：远端尚未实现时不宣称可以截取屏幕。"""
        _, lease = self._registry.interaction_snapshot()
        return bool(lease is not None and lease.device_id == "local" and self._local.available)

    def available_for(self, device_id: str | None = None) -> bool:
        """available_for（指定设备能力）：只报告当前真实可用的本地截图。"""
        if device_id is None:
            return self.available
        connection = self._registry.connection(device_id)
        return bool(device_id == "local" and connection is not None and connection.connected and self._local.available)

    async def grab(self, *, source: str | None = None, scope: str | None = None,
                   device_id: str | None = None) -> Any:
        """grab（捕获）：绑定当前交互租约，切换期间丢弃旧设备返回值。"""
        with self._lock:
            generation, lease = self._registry.interaction_snapshot()
            if device_id is None and lease is None:
                raise RuntimeError("当前没有获得语音交互权的设备")
            selected = device_id if device_id is not None else lease.device_id
            connection = self._registry.connection(selected)
            if connection is None or not connection.connected:
                raise RuntimeError("截图目标设备未连接")
            target = (connection, generation if device_id is None else None)
            key = (source, scope)
            if self._targets.get(key) != target:
                self._local.invalidate(source=source, scope=scope)
                self._targets[key] = target
            self._targets.move_to_end(key)
            if len(self._targets) > 1024:
                (old_source, old_scope), _ = self._targets.popitem(last=False)
                self._local.invalidate(source=old_source, scope=old_scope)
            if selected != "local":
                raise RuntimeError("当前交互设备的屏幕捕获尚不可用")
        packet = await self._local.grab(source=source, scope=scope)
        with self._lock:
            if (self._registry.connection(selected) != connection
                    or (device_id is None and not self._registry.accepts(lease))):
                if self._targets.get(key) == target:
                    self._local.invalidate(source=source, scope=scope)
                raise RuntimeError("屏幕捕获期间交互设备已变化")
        return {**packet, "execution_device_id": selected}  # execution_device_id（截图实际执行设备）。
