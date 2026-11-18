"""DesktopControl（桌面控制管道）：仅外壳管理入口进程，业务仍由宿主管理。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from uuid import uuid4

MAX_CONTROL_MESSAGE_BYTES = 512 * 1024  # MAX_CONTROL_MESSAGE_BYTES（父子宿主消息上限）。
MAX_DEVICE_ARGUMENT_BYTES = 256 * 1024  # MAX_DEVICE_ARGUMENT_BYTES（桌面操作参数上限）。
DEVICE_HELPER_OPERATIONS = frozenset({
    "windows", "apps", "snapshot", "shot", "tree", "click", "type", "scroll", "key", "move",
    "windowControl", "invokePattern", "readText", "readTable", "clipboard", "process", "displays",
    "cursorPosition", "notify", "file.list", "file.read", "file.stat", "terminal.create", "terminal.input",
    "terminal.output", "terminal.status", "terminal.cancel", "terminal.close",
})


class DesktopControl:
    def __init__(self, application) -> None:
        self.application = application
        self._write_lock = threading.Lock()
        self._pending_device_calls: dict[str, asyncio.Future] = {}
        self._pending_device_calls_lock = threading.Lock()
        self.owned = asyncio.Event()  # owned（外壳已接管）：进入作业对象前不启动业务子进程。
        application.exit_callback = lambda: self.send("exit_requested")
        application.desktop_control = self  # desktop_control（Tauri 与核心之间受管 RPC）。

    def send(self, kind: str, **values) -> None:
        # kind（消息类型）；协议只写私有标准输出，业务日志写独立文件。
        with self._write_lock:
            try:
                sys.stdout.write(json.dumps({"protocol": 1, "kind": kind, **values}, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except (OSError, BrokenPipeError):
                self.stop(0)

    def start(self, origin: str, launch_token: str, *, continuity: dict | None = None) -> None:
        # ready（就绪消息）字段：origin（界面来源）、launch_token（一次性入口令牌）、pid（进程标识）、instance_id（实例标识）。
        # launch_token（一次性桌面入口令牌）只走外壳私有标准输出控制管道，不进入普通日志或启动登记。
        self.send("ready", origin=origin, launch_token=launch_token, pid=os.getpid(),
                  instance_id=self.application.state.value["instance_id"], continuity=continuity)
        threading.Thread(target=self._read, name="desktop-control", daemon=True).start()

    async def call_device_helper(self, payload: dict) -> dict:
        """call_device_helper（受管设备操作）：只接收 DSH 已验证且已获批的单次请求。"""
        if not isinstance(payload, dict) or set(payload) != {
            "request_id", "device_id", "operation", "arguments", "timeout_ms",
            "connection_generation", "executor_generation",
        }:
            raise ValueError("桌面设备请求字段无效。")
        request_id = payload["request_id"]
        device_id = payload["device_id"]
        operation = payload["operation"]
        arguments = payload["arguments"]
        timeout_ms = payload["timeout_ms"]
        connection_generation = payload["connection_generation"]
        executor_generation = payload["executor_generation"]
        if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id)
                or device_id != "local" or operation not in DEVICE_HELPER_OPERATIONS
                or not isinstance(arguments, dict) or type(timeout_ms) is not int or not 1 <= timeout_ms <= 600_000
                or type(connection_generation) is not int or connection_generation < 1
                or type(executor_generation) is not int or executor_generation < 1):
            raise ValueError("桌面设备编号、操作、参数或时限无效。")
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("桌面设备请求无法编码。") from exc
        if len(encoded) > MAX_DEVICE_ARGUMENT_BYTES:
            raise ValueError("桌面设备请求超过大小限制。")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._pending_device_calls_lock:
            if request_id in self._pending_device_calls:
                raise ValueError("桌面设备请求编号正在使用。")
            self._pending_device_calls[request_id] = future
        try:
            self.send("device_helper_request", **payload)
            try:
                return await asyncio.wait_for(future, timeout=timeout_ms / 1000 + 5)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("桌面操作回执超时，执行结果未知；查询原任务后再决定后续操作。") from exc
        finally:
            with self._pending_device_calls_lock:
                self._pending_device_calls.pop(request_id, None)

    async def _set_device_gateway(self, request_id: int, enabled: bool) -> None:
        try:
            result = await self.application.set_device_gateway(enabled)
            self.send("device_gateway_result", request_id=request_id, ok=True, status=result)
        except Exception as exc:
            self.send("device_gateway_result", request_id=request_id, ok=False, error=str(exc)[:240])

    def _settle_device_helper(self, message: dict) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._pending_device_calls_lock:
            future = self._pending_device_calls.pop(request_id, None)
        if future is None or future.done():
            return
        if message.get("ok") is not True or not isinstance(message.get("response"), dict):
            detail = message.get("error")
            future.set_exception(RuntimeError(detail if isinstance(detail, str) else "桌面操作结果未知。"))
            return
        future.set_result(message["response"])

    def _fail_pending_device_calls(self) -> None:
        with self._pending_device_calls_lock:
            pending = list(self._pending_device_calls.values())
            self._pending_device_calls.clear()
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError("Tauri 桌面设备控制通道已关闭，操作结果未知。"))

    def stop(self, budget_ms: int = 14000) -> None:
        self.application.shutdown_deadline = time.monotonic() + max(0, min(14000, budget_ms)) / 1000
        self.application.loop.call_soon_threadsafe(self.application.exit_event.set)
        self.application.loop.call_soon_threadsafe(self.owned.set)

    def _read(self) -> None:
        try:
            while True:
                line = sys.stdin.readline(MAX_CONTROL_MESSAGE_BYTES + 1)
                if not line:
                    break  # 父进程退出／管道关闭也进入同一退出流程。
                if len(line.encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                kind = message.get("kind")
                if message.get("protocol") != 1:
                    break
                if kind == "start":
                    self.application.loop.call_soon_threadsafe(self.owned.set)
                if kind == "shutdown":
                    self.stop(int(message.get("budget_ms", 14000)))
                    return
                if kind == "set_device_gateway":
                    if type(message.get("request_id")) is int and type(message.get("enabled")) is bool:
                        asyncio.run_coroutine_threadsafe(
                            self._set_device_gateway(message["request_id"], message["enabled"]), self.application.loop)
                    continue
                if kind == "device_helper_response":
                    loop = self.application.loop
                    if loop is not None:
                        loop.call_soon_threadsafe(self._settle_device_helper, message)
                    continue
                if kind == "flush_logs" and type(message.get("request_id")) is int:
                    # request_id（本次刷新请求编号）用于将刷新结果对应到原生桌面调用。
                    self._flush_logs(message["request_id"])
                if kind == "desktop_process" and type(message.get("pid")) is int and message["pid"] > 0:
                    state = self.application.state
                    with state.condition:
                        if not any(row["id"] == "desktop" for row in state.value["components"]):
                            row = state.new_component("host")
                            row.update(id="desktop", name="桌面窗口与渲染进程", required=False, shared_with=None)
                            state.value["components"].append(row)
                    state.component("desktop", state="ready", phase="窗口已建立", pid=message["pid"],
                                    created_at=message.get("created_at"))
        except (ValueError, TypeError, OSError):
            pass
        loop = self.application.loop
        if loop is not None:
            loop.call_soon_threadsafe(self._fail_pending_device_calls)
        self.stop()

    def _flush_logs(self, request_id: int) -> None:
        # handlers（日志处理器）：逐个刷新进程内文件日志并提交到磁盘。
        handlers = {id(handler): handler for handler in logging.getLogger().handlers}
        for logger in logging.Logger.manager.loggerDict.values():
            if isinstance(logger, logging.Logger):
                handlers.update({id(handler): handler for handler in logger.handlers})
        try:
            from ..logging_setup import flush_audit_logs
            flush_audit_logs()
            for handler in handlers.values():
                handler.acquire()
                try:
                    handler.flush()
                    if isinstance(handler, logging.FileHandler):
                        stream = handler.stream
                        if stream is not None and not stream.closed:
                            os.fsync(stream.fileno())
                finally:
                    handler.release()
        except Exception:
            self.send("logs_flush_failed", request_id=request_id)
        else:
            self.send("logs_flushed", request_id=request_id)
