"""DesktopControl（桌面控制管道）：仅外壳管理入口进程，业务仍由宿主管理。"""
from __future__ import annotations

import asyncio
import json
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

    def start(self, origin: str) -> None:
        self.send("ready", origin=origin, pid=os.getpid(), instance_id=self.application.state.value["instance_id"])
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
