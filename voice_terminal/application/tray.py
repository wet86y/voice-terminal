"""System tray（系统托盘）：与应用宿主共享真实状态和退出入口。"""
from __future__ import annotations

import os
import threading
import webbrowser


class ApplicationTray:
    def __init__(self, application, origin):
        import pystray
        from PIL import Image, ImageDraw
        self.application = application
        self.origin = origin
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill="#242b3e")
        for x, height in ((16, 12), (24, 26), (32, 40), (40, 26), (48, 12)):
            draw.rounded_rectangle((x-2, 32-height//2, x+2, 32+height//2), radius=2, fill="#8de2bb")
        self.icon = pystray.Icon("VoiceTerminal", image, "Voice Terminal · 启动中", pystray.Menu(
            pystray.MenuItem("打开工作台", lambda *_: self.open(), default=True),
            pystray.MenuItem("系统运行", lambda *_: self.open(system=True)),
            pystray.MenuItem("打开日志目录", lambda *_: os.startfile(str(application.root / "logs"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出应用", lambda *_: application.request_exit()),
        ))

    def open(self, *, system=False):
        webbrowser.open(self.origin + ("/?settings=system" if system else "/"))

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True, name="application-tray").start()

    def refresh(self):
        state = self.application.state.snapshot()["state"]
        self.icon.title = "Voice Terminal · " + {"starting": "启动中", "ready": "正常运行", "degraded": "降级运行", "failed": "需要检查", "stopping": "退出中", "stopped": "已退出"}[state]

    def close(self):
        self.icon.stop()
