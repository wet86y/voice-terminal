"""Screen routing（屏幕捕获路由）：主语音与代理共享固定的设备目标。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from threading import RLock
from typing import Any
from collections import OrderedDict
from uuid import uuid4

from ..screen_capture import screen_packet_json
from .file_transfer import DeviceFileTransferStore, device_file_transfer_id
from .registry import DeviceRegistry


class ScreenCaptureRouter:
    """ScreenCaptureRouter（屏幕路由）：按当前交互设备选择本机或远端执行，绝不回退。"""

    def __init__(self, local: Any, registry: DeviceRegistry, remote: Any = None,
                 files: DeviceFileTransferStore | None = None) -> None:
        self._local = local
        self._registry = registry
        self._remote = remote
        self._files = files
        self._lock = RLock()
        self._targets: OrderedDict[tuple[str | None, str | None], tuple] = OrderedDict()

    def __getattr__(self, name: str) -> Any:
        # 配置、来源租约和消费规则继续由既有截图服务维护。
        return getattr(self._local, name)

    def _remote_ready(self, device_id: str) -> bool:
        """_remote_ready（远端截图能力）：需要已认证任务通道和桌面执行器。"""
        if self._remote is None or self._files is None:
            return False
        connection = self._registry.connection(device_id)
        return bool(connection is not None and connection.connected
                    and {"screen_capture", "file_transfer"}.issubset(connection.capabilities))

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
                   device_id: str | None = None, binary: bool = False) -> Any:
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
            packet = await self._local.grab(source=source, scope=scope, binary=binary)
        else:
            if not self._remote_ready(selected):
                raise RuntimeError("远端设备屏幕捕获不可用")
            packet = await self._grab_remote(selected, connection, source=source, scope=scope)
            if not binary:
                packet = await asyncio.to_thread(screen_packet_json, packet)
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
        generation = self._local.generation
        lease_generation = self._local._lease_generation(lease_key)
        started = time.monotonic()
        packet = await self._capture_remote(device_id, connection, self._local.config)
        if (generation != self._local.generation
                or lease_generation != self._local._lease_generation(lease_key)):
            raise RuntimeError("屏幕捕获期间请求作用域已失效")
        packet["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        self._local._remember_unconsumed(lease_key, packet, generation, lease_generation)
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
        """_capture_remote（远端双图）：小回执绑定独立文件通道中的原始图像。"""
        capture_id = uuid4().hex
        ticket = {
            "transfer_id": device_file_transfer_id(connection, capture_id),
            "device_id": device_id,
            "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation,
        }
        self._files.prepare_screen_upload(**ticket)
        try:
            record = await self._remote.execute(
                request_id=capture_id, device_id=device_id, operation="screen.capture_file",
                arguments={"transfer_id": ticket["transfer_id"], "max_side": config.global_max_edge,
                           "crop_width": config.crop_width, "crop_height": config.crop_height},
                timeout_seconds=45.0, expected_connection=connection,
            )
            result = record.result
            if record.status != "succeeded":
                detail = result.get("error") if isinstance(result, dict) else None
                raise RuntimeError(f"远端屏幕捕获失败：{detail[:240] if isinstance(detail, str) else '未取得成功回执'}")
            if not isinstance(result, dict):
                raise RuntimeError("远端双图回执格式无效")
            if result.get("transfer_id") != ticket["transfer_id"]:
                raise RuntimeError("远端双图回执传输编号无效")
        except BaseException:
            self._files.close(**ticket)
            raise
        read = asyncio.create_task(asyncio.to_thread(
            self._files.consume_screen_upload, **ticket,
            expected_bytes=result.get("bytes"), expected_sha256=result.get("sha256"),
        ))
        # 即使外层取消，也等读句柄释放后回收原票据与双图暂存。
        read.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)
        raw = await asyncio.shield(read)
        return await asyncio.to_thread(_capture_packet, capture_id, config, result.get("capture"), raw)


def _capture_packet(capture_id: str, config: Any, capture: Any, raw: bytes) -> dict[str, Any]:
    """_capture_packet（双图封装）：按回执字节偏移拆分，复核图像格式与尺寸。"""
    from PIL import Image

    keys = ("monitor_width", "monitor_height", "center_x", "center_y", "crop_left", "crop_top",
            "crop_width", "crop_height", "global_bytes")
    if not isinstance(capture, dict) or any(type(capture.get(key)) is not int for key in keys):
        raise RuntimeError("远端双图元数据无效")
    width, height, x, y, left, top, crop_width, crop_height, split = (capture[key] for key in keys)
    if (not 0 <= x < width or not 0 <= y < height
            or not 0 <= left < left + crop_width <= width or not 0 <= top < top + crop_height <= height
            or crop_width != min(config.crop_width, width) or crop_height != min(config.crop_height, height)
            or not 0 < split < len(raw)):
        raise RuntimeError("远端双图边界无效")
    center_source = capture.get("center_source")
    executable = capture.get("executable")
    if center_source not in ("window_center", "screen_center") or not isinstance(executable, str):
        raise RuntimeError("远端双图来源无效")
    crop_rect = (left, top, left + crop_width, top + crop_height)
    attachments = []
    for role, image_bytes, fmt, mime in (("global", raw[:split], "JPEG", "image/jpeg"),
                                         ("local", raw[split:], "PNG", "image/png")):
        try:
            with Image.open(io.BytesIO(image_bytes), formats=[fmt]) as image:
                image_width, image_height = image.size
                if role == "global":
                    if max(image.size) != min(max(width, height), config.global_max_edge):
                        raise ValueError("全局图尺寸无效")
                elif image.size != (crop_width, crop_height):
                    raise ValueError("局部图尺寸无效")
                image.verify()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise RuntimeError("远端双图格式或尺寸无效") from exc
        attachments.append({
            "attachment_id": f"{capture_id}-{role}", "role": role, "mime_type": mime,
            "width": image_width, "height": image_height, "bytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
            "data": image_bytes,  # data（原始图片字节）。
        })
    return {
        "capture_id": capture_id, "captured_at": time.time(),
        "monitor_bounds": (0, 0, width, height), "executable": executable,
        "center_point": (x, y), "crop_rect": crop_rect, "valid_region": crop_rect,
        "center_source": center_source, "fullscreen": False,
        "lock_inferred": False, "cursor_visible": False,
        "cursor_in_monitor": False, "capture_detected": False,
        "clip_bounds": None, "notification_state": None, "exclusive": None,
        "attachments": attachments,
    }
