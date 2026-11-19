"""Screen routing（屏幕捕获路由）：主语音与代理共享固定的设备目标。"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from threading import RLock
from typing import Any
from collections import OrderedDict
from uuid import uuid4

from .registry import DeviceRegistry


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class ScreenCaptureRouter:
    """ScreenCaptureRouter（屏幕路由）：按当前交互设备选择本机或远端执行，绝不回退。"""

    def __init__(self, local: Any, registry: DeviceRegistry, remote: Any = None) -> None:
        self._local = local
        self._registry = registry
        self._remote = remote
        self._lock = RLock()
        self._targets: OrderedDict[tuple[str | None, str | None], tuple] = OrderedDict()

    def __getattr__(self, name: str) -> Any:
        # 配置、来源租约和消费规则继续由既有截图服务维护。
        return getattr(self._local, name)

    def _remote_ready(self, device_id: str) -> bool:
        """_remote_ready（远端截图能力）：需要已认证任务通道和桌面执行器。"""
        if self._remote is None:
            return False
        connection = self._registry.connection(device_id)
        return bool(connection is not None and connection.connected
                    and "desktop_control" in connection.capabilities)

    @property
    def available(self) -> bool:
        """available（目标能力）：按当前语音交互设备报告真实可用的截图。"""
        _, lease = self._registry.interaction_snapshot()
        if lease is None:
            return False
        if lease.device_id == "local":
            return bool(self._local.available)
        return self._remote_ready(lease.device_id)

    def available_for(self, device_id: str | None = None) -> bool:
        """available_for（指定设备能力）：本机走本地捕获，远端走已认证设备执行器。"""
        if device_id is None:
            return self.available
        if device_id == "local":
            connection = self._registry.connection(device_id)
            return bool(connection is not None and connection.connected and self._local.available)
        return self._remote_ready(device_id)

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
        if selected == "local":
            packet = await self._local.grab(source=source, scope=scope)
        else:
            if not self._remote_ready(selected):
                raise RuntimeError("远端设备屏幕捕获不可用")
            packet = await self._grab_remote(selected, connection, source=source, scope=scope)
        with self._lock:
            if (self._registry.connection(selected) != connection
                    or (device_id is None and not self._registry.accepts(lease))):
                if self._targets.get(key) == target:
                    self._local.invalidate(source=source, scope=scope)
                raise RuntimeError("屏幕捕获期间交互设备已变化")
        return {**packet, "execution_device_id": selected}  # execution_device_id（截图实际执行设备）。

    async def _grab_remote(self, device_id: str, connection: Any, *,
                           source: str | None, scope: str | None) -> dict[str, Any]:
        """_grab_remote（远端捕获）：复用既有未消费租约，只把执行位置换到固定设备。"""
        if self._remote is None:
            raise RuntimeError("远端设备任务通道不可用")
        lease_key = self._local._lease_key(source, scope)
        pending = self._local._unconsumed_packet(lease_key)
        if pending is not None:
            return pending
        started = time.monotonic()
        packet = await self._capture_remote(device_id, connection, self._local.config)
        packet["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        self._local._remember_unconsumed(
            lease_key, packet, self._local.generation,
            self._local._lease_generation(lease_key),
        )
        # 日志只含尺寸、字节数和摘要，绝不输出图片编码。
        logging.getLogger(__name__).info(
            "screen_capture（远端屏幕捕获） device=%s elapsed_ms=%s images=%s",
            device_id, packet["elapsed_ms"],
            [{key: item[key] for key in ("role", "width", "height", "bytes", "sha256")}
             for item in packet.get("attachments", [])],
        )
        return packet

    async def _capture_remote(self, device_id: str, connection: Any,
                              config: Any) -> dict[str, Any]:
        """_capture_remote（远端双图）：前台窗口快照加整屏图，按本机同一套双图规则编码。"""
        from PIL import Image
        window_state = await self._remote_operation(
            device_id, connection, "tree",
            {"maxElements": 1, "maxDepth": 1, "includePixels": False}, 10.0,
        )
        window = window_state.get("snapshot") or {}
        if not isinstance(window, dict):
            window = {}
        shot = await self._remote_operation(
            device_id, connection, "shot",
            {"wholeScreen": True, "maxSide": int(config.global_max_edge)}, 20.0,
        )
        try:
            png = base64.b64decode(str(shot["pngBase64"]), validate=True)
            image = Image.open(io.BytesIO(png)).convert("RGB")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("远端屏幕图像格式无效") from exc
        bounds = shot.get("snapshot") or {}
        bounds = bounds.get("rect") if isinstance(bounds, dict) else None
        bounds = bounds if isinstance(bounds, dict) else {}
        physical_width = int(bounds.get("width") or image.width)
        physical_height = int(bounds.get("height") or image.height)
        if physical_width <= 0 or physical_height <= 0:
            physical_width, physical_height = image.width, image.height
        scale_x = image.width / physical_width
        scale_y = image.height / physical_height
        rect = window.get("rect") if isinstance(window.get("rect"), dict) else {}
        center_x = int(rect.get("x") or 0) + int(rect.get("width") or 0) // 2
        center_y = int(rect.get("y") or 0) + int(rect.get("height") or 0) // 2
        # 前台窗口不在整屏图范围内时以屏幕中心为局部原点，不跨屏伪造坐标。
        if 0 <= center_x < physical_width and 0 <= center_y < physical_height:
            center_source = "window_center"
        else:
            center_x, center_y = physical_width // 2, physical_height // 2
            center_source = "screen_center"
        crop_width = min(int(config.crop_width), physical_width)
        crop_height = min(int(config.crop_height), physical_height)
        left = _bounded(center_x - crop_width // 2, 0, physical_width - crop_width)
        top = _bounded(center_y - crop_height // 2, 0, physical_height - crop_height)
        right, bottom = left + crop_width, top + crop_height
        box = (round(left * scale_x), round(top * scale_y),
               round(right * scale_x), round(bottom * scale_y))
        local = image.crop(box)
        full = image.copy()
        full.thumbnail((int(config.global_max_edge), int(config.global_max_edge)))
        capture_id = uuid4().hex
        attachments = []
        for role, frame, fmt, mime in (("global", full, "JPEG", "image/jpeg"),
                                       ("local", local, "PNG", "image/png")):
            stream = io.BytesIO()
            frame.save(stream, format=fmt, **({"quality": 85} if fmt == "JPEG" else {}))
            raw = stream.getvalue()
            attachments.append({
                "attachment_id": f"{capture_id}-{role}", "role": role, "mime_type": mime,
                "width": frame.width, "height": frame.height, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "base64": base64.b64encode(raw).decode("ascii"),
            })
        return {
            "capture_id": capture_id, "captured_at": time.time(),
            "monitor_bounds": (0, 0, physical_width, physical_height),
            "executable": str(window.get("executablePath") or ""),
            "center_point": (center_x, center_y),
            "crop_rect": (left, top, right, bottom),
            "valid_region": (left, top, right, bottom),
            "center_source": center_source, "fullscreen": False,
            "lock_inferred": False, "cursor_visible": False,
            "cursor_in_monitor": False, "capture_detected": False,
            "clip_bounds": None, "notification_state": None, "exclusive": None,
            "attachments": attachments,
        }

    async def _remote_operation(self, device_id: str, connection: Any, operation: str,
                                arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        """_remote_operation（固定设备操作）：结果未知交给原任务查询，不自动重发。"""
        record = await self._remote.execute(
            request_id=uuid4().hex, device_id=device_id, operation=operation,
            arguments=arguments, timeout_seconds=timeout, expected_connection=connection,
        )
        if getattr(record, "status", None) != "succeeded":
            raise RuntimeError(f"远端 {operation}（设备操作）未成功")
        result = getattr(record, "result", None)
        if not isinstance(result, dict):
            raise RuntimeError(f"远端 {operation}（设备操作）回执格式无效")
        return result
