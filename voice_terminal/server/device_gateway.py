"""DeviceGateway（设备回环网关）：只交接监听与远端连接，不触碰语音核心的生命周期。"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext

import uvicorn

from ..application.ports import bind_loopback
from ..application.managed import report_endpoint
from ..application.ports import endpoint_url


class DeviceGateway:
    def __init__(self, app, runtime, *, port: int = 0) -> None:
        self.app = app
        self.runtime = runtime
        self.port = port  # port（监听请求端口；默认由系统分配）。
        self.lock = asyncio.Lock()  # lock（开关和退出共用的监听交接锁）。
        self.server = None
        self.task = None
        self.socket = None

    async def set_enabled(self, enabled: bool) -> dict:
        async with self.lock:
            self.runtime._device_gateway_enabled = enabled
            if enabled and self.task is not None and not self.task.done():
                return self.status()
            if not enabled:
                await self._stop()
                self.runtime._device_gateway_bind_error = None
                return self.status()
            if self.task is not None or self.socket is not None:
                await self._stop()
            try:
                listener, actual_port = bind_loopback(self.port, host="127.0.0.1")
                self.socket = listener
                # 路径白名单必须在开始处理请求前绑定实际端口。
                self.runtime._device_gateway_port = actual_port

                class GatewayServer(uvicorn.Server):
                    def capture_signals(self):
                        # signal（退出信号）仍由主服务统一管理，附属监听不重复注册。
                        return nullcontext()

                self.server = GatewayServer(uvicorn.Config(
                    self.app, lifespan="off", log_config=None, access_log=False,
                    timeout_graceful_shutdown=1, ws_max_size=270_336,
                    host="127.0.0.1", port=actual_port,
                ))
                self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
                async with asyncio.timeout(3):
                    while not self.server.started:
                        if self.task.done():
                            await self.task
                            raise RuntimeError("设备回环网关监听未建立。")
                        await asyncio.sleep(.01)
                self.runtime._device_gateway_listener_socket = listener
                self.runtime._device_gateway_bind_error = None
                report_endpoint("gateway", endpoint_url("gateway", actual_port))
            except Exception as exc:
                await self._stop()
                self.runtime._device_gateway_bind_error = str(exc)[:240] or type(exc).__name__
            return self.status()

    def status(self) -> dict:
        return {
            "enabled": self.runtime._device_gateway_enabled,
            "ready": self.socket is not None and self.task is not None and not self.task.done(),
            "error": self.runtime._device_gateway_bind_error,
        }

    async def _stop(self) -> None:
        self.runtime._device_gateway_listener_socket = None
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 3)
            except (Exception, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.socket is not None:
            self.socket.close()
        self.socket = self.server = self.task = None
        self.runtime._device_gateway_port = None
