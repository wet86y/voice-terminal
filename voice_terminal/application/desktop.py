"""DesktopControl（桌面控制管道）：仅外壳管理入口进程，业务仍由宿主管理。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time


class DesktopControl:
    def __init__(self, application) -> None:
        self.application = application
        self._write_lock = threading.Lock()
        self.owned = asyncio.Event()  # owned（外壳已接管）：进入作业对象前不启动业务子进程。
        application.exit_callback = lambda: self.send("exit_requested")

    def send(self, kind: str, **values) -> None:
        # kind（消息类型）；协议只写私有标准输出，业务日志写独立文件。
        with self._write_lock:
            try:
                sys.stdout.write(json.dumps({"protocol": 1, "kind": kind, **values}, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except (OSError, BrokenPipeError):
                self.stop(0)

    def start(self, origin: str, launch_token: str) -> None:
        # ready（就绪消息）字段：origin（界面来源）、launch_token（一次性入口令牌）、pid（进程标识）、instance_id（实例标识）。
        # launch_token（一次性桌面入口令牌）只走外壳私有标准输出控制管道，不进入普通日志或启动登记。
        self.send("ready", origin=origin, launch_token=launch_token, pid=os.getpid(),
                  instance_id=self.application.state.value["instance_id"])
        threading.Thread(target=self._read, name="desktop-control", daemon=True).start()

    def stop(self, budget_ms: int = 14000) -> None:
        self.application.shutdown_deadline = time.monotonic() + max(0, min(14000, budget_ms)) / 1000
        self.application.loop.call_soon_threadsafe(self.application.exit_event.set)
        self.application.loop.call_soon_threadsafe(self.owned.set)

    def _read(self) -> None:
        try:
            while True:
                line = sys.stdin.readline(16385)
                if not line:
                    break  # 父进程退出／管道关闭也进入同一退出流程。
                if len(line) > 16384:
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
        self.stop()

    def _flush_logs(self, request_id: int) -> None:
        # handlers（日志处理器）：逐个刷新进程内文件日志并提交到磁盘。
        handlers = {id(handler): handler for handler in logging.getLogger().handlers}
        for logger in logging.Logger.manager.loggerDict.values():
            if isinstance(logger, logging.Logger):
                handlers.update({id(handler): handler for handler in logger.handlers})
        try:
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
