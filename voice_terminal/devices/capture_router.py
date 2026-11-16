"""Selectable capture route（可切换采集路由）：固定控制器引用，串行更换设备端口。"""
from __future__ import annotations

import asyncio

from ..controller.turn_workspace import AudioFrame
from .contracts import CapturePort


class CaptureRouteUnavailable(RuntimeError):
    """CaptureRouteUnavailable（当前没有可用的交互设备采集端口）。"""


class SelectableCapturePort:
    """SelectableCapturePort（可切换采集端口）：控制器持有不变，设备实现可被显式替换。

    The route does not choose a fallback when a remote port fails or is removed.
    The caller must select a new port only after the interaction lease is granted.
    """

    def __init__(self, local_port: CapturePort) -> None:
        self._local_port: CapturePort | None = local_port  # _local_port（显式切回本机时使用的采集端口）。
        self._active_port: CapturePort | None = local_port  # _active_port（当前控制器实际读取的端口）。
        self._active_device_id: str | None = "local"  # _active_device_id（当前交互采集设备编号）。
        self._running = False  # _running（路由是否已启动）。
        self._generation = 0  # _generation（端口选择代次，拒绝旧端口迟到帧）。
        self._changed = asyncio.Event()  # _changed（唤醒等待旧端口帧的读取者）。
        self._switch_lock = asyncio.Lock()  # _switch_lock（串行化采集目标切换）。

    @property
    def active_device_id(self) -> str | None:
        """active_device_id（当前显式选择的交互设备编号）。"""
        return self._active_device_id

    @property
    def running(self) -> bool:
        """running（路由是否已启动且当前设备正在采集）。"""
        return self._running and self._active_port is not None and self._active_port.running

    @property
    def diagnostics(self) -> dict:
        """diagnostics（当前端口的有界状态快照）。"""
        port = self._active_port
        if port is None:
            return {"active_device_id": None, "running": False, "route_generation": self._generation}
        result = dict(port.diagnostics)
        result.update(active_device_id=self._active_device_id, route_generation=self._generation)
        return result

    def set_local_port(self, port: CapturePort | None) -> None:
        """set_local_port（替换本地设备实现）：只允许在采集停止时重建。"""
        if self._running:
            raise RuntimeError("本地采集器只能在语音采集停止时重建。")
        self._local_port = port
        if self._active_device_id == "local":
            self._active_port = port
            self._notify_changed()

    async def select(self, device_id: str, port: CapturePort | None) -> None:
        """select（显式切换采集目标）：先停旧端口，再启动新端口，不自动回退。"""
        if not isinstance(device_id, str) or not device_id or device_id != device_id.strip():
            raise ValueError("交互设备编号无效。")
        if (device_id == "local") != (port is self._local_port):
            raise ValueError("采集端口和设备编号不匹配。")
        if port is not None and not self._is_capture_port(port):
            raise TypeError("目标设备不符合采集端口契约。")

        async with self._switch_lock:
            if self._active_device_id == device_id and self._active_port is port:
                return
            previous_port = self._active_port
            previous_device_id = self._active_device_id
            was_running = self._running
            self._active_port = port
            self._active_device_id = device_id if port is not None else None
            self._notify_changed()
            if not was_running:
                return

            try:
                if previous_port is not None:
                    await previous_port.stop()
                if port is None:
                    self._running = False
                    return
                await port.start()
            except Exception:
                if port is not None:
                    try:
                        await port.stop()
                    except Exception:
                        pass
                self._active_port = None
                self._active_device_id = None
                self._running = False
                self._notify_changed()
                raise

    async def select_local(self) -> None:
        """select_local（显式切回本机）：不由远端断线自动触发。"""
        if self._local_port is None:
            raise CaptureRouteUnavailable("本机采集设备尚未就绪。")
        await self.select("local", self._local_port)

    async def clear_if_selected(self, device_id: str, port: CapturePort) -> bool:
        """clear_if_selected（清除指定远端流）：迟到断线不能清除新流。"""
        async with self._switch_lock:
            if self._active_device_id != device_id or self._active_port is not port:
                return False
            was_running = self._running
            self._active_port = None
            self._active_device_id = None
            self._running = False
            self._notify_changed()
            if was_running:
                await port.stop()
            return True

    async def deactivate(self, device_id: str) -> bool:
        """deactivate（撤销当前设备采集）：仅清除所指设备，不自动切回本机。"""
        async with self._switch_lock:
            if self._active_device_id != device_id:
                return False
            port = self._active_port
            was_running = self._running
            self._active_port = None
            self._active_device_id = None
            self._running = False
            self._notify_changed()
            if was_running and port is not None:
                await port.stop()
            return True

    async def start(self) -> None:
        """start（启动当前端口）：沿用核心控制器现有的会话启停入口。"""
        async with self._switch_lock:
            if self._running:
                return
            port = self._active_port
            if port is None:
                raise CaptureRouteUnavailable("当前交互设备没有可用采集端口。")
            await port.start()
            self._running = True

    async def stop(self) -> None:
        """stop（停止当前端口）：唤醒正在等待的帧读取并释放设备资源。"""
        async with self._switch_lock:
            if not self._running:
                return
            port = self._active_port
            self._running = False
            self._notify_changed()
            if port is not None:
                await port.stop()

    async def read_frame(self) -> AudioFrame:
        """read_frame（读取当前设备帧）：路由切换时丢弃旧端口尚未返回的帧。"""
        while True:
            if not self._running:
                raise CaptureRouteUnavailable("当前交互设备采集尚未启动。")
            port = self._active_port
            if port is None:
                raise CaptureRouteUnavailable("当前交互设备没有可用采集端口。")
            generation = self._generation
            changed = self._changed
            reader = asyncio.create_task(port.read_frame())
            switch_waiter = asyncio.create_task(changed.wait())
            try:
                done, _ = await asyncio.wait(
                    (reader, switch_waiter), return_when=asyncio.FIRST_COMPLETED,
                )
                if switch_waiter in done or generation != self._generation:
                    continue
                frame = await reader
                if generation == self._generation:
                    return frame
            finally:
                for task in (reader, switch_waiter):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(reader, switch_waiter, return_exceptions=True)

    def read(self) -> bytes:
        """read（同步兼容入口）：异步采集契约不支持同步阻塞读取。"""
        raise RuntimeError("可切换采集端口只支持异步 read_frame()。")

    def pending_frame_count(self) -> int:
        """pending_frame_count（待处理帧数）：读取当前端口队列深度。"""
        port = self._active_port
        return 0 if port is None else port.pending_frame_count()

    def read_pending_frame(self) -> AudioFrame:
        """read_pending_frame（读取预录帧）：保持既有唤醒预录调用语义。"""
        port = self._active_port
        if port is None:
            raise CaptureRouteUnavailable("当前交互设备没有可用采集端口。")
        return port.read_pending_frame()

    def preroll(self, milliseconds: int) -> bytes:
        """preroll（读取唤醒预录）：固定从当前交互设备的缓冲读取。"""
        port = self._active_port
        if port is None:
            return b""
        return port.preroll(milliseconds)

    def _notify_changed(self) -> None:
        previous = self._changed
        self._changed = asyncio.Event()
        self._generation += 1
        previous.set()

    @staticmethod
    def _is_capture_port(port: object) -> bool:
        return all(hasattr(port, name) for name in (
            "running", "diagnostics", "start", "stop", "read_frame",
            "pending_frame_count", "read_pending_frame", "preroll",
        ))
