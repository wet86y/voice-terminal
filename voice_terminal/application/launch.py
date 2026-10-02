"""Standalone launcher（独立启动入口）：托盘与网页优先，业务组件随后启动。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
import webbrowser

from .supervisor import ApplicationHost
from .web import HostWeb
from .windows import SingleInstance


def find_root(explicit: str = "") -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "config/default.yaml").is_file():
            raise RuntimeError("指定目录不是 Voice Terminal 项目")
        return root
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for root in source.parents:
        if (root / "config/default.yaml").is_file() and (root / "package.json").is_file():
            return root
    raise RuntimeError("未找到项目根目录，请使用 --root 指定项目")


async def run(args, root):
    app = ApplicationHost(root, python=args.python, config=args.config, no_worker=args.no_worker,
        no_analysis=args.no_analysis, no_web=args.no_web, llm_provider=args.llm_provider)
    app.loop = asyncio.get_running_loop()
    app.dev = args.dev
    web = HostWeb(app, root / "apps/web/dist", port=8764 if args.dev else 5173)
    app.origin = web.origin
    web.start()
    for key in ("host", "web"):
        app.state.component(key, state="ready", phase="入口已就绪", pid=os.getpid(), created_at=time.time())
    if not args.dev and not args.no_web and not (root / "apps/web/dist/index.html").is_file():
        app.state.component("web", state="failed", phase="使用内置诊断页",
            error="缺少网页构建产物，请执行 npm run build（构建网页）。")
    registry = root / ".runtime/application/instance.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    browser_origin = web.origin
    registry.write_text(json.dumps({"pid": os.getpid(), "url": browser_origin}), encoding="utf-8")
    tray = None
    try:
        if not args.no_tray:
            from .tray import ApplicationTray
            try:
                tray = ApplicationTray(app, browser_origin)
                tray.start()
            except Exception:
                logging.exception("系统托盘建立失败")
                app.state.component("host", error="托盘不可用；可通过网页退出应用")
        if args.dev:
            import shutil
            try:
                node = shutil.which("node")
                if not node:
                    raise RuntimeError("开发模式缺少 Node.js（JavaScript 运行环境）")
                if app.port_busy("http://127.0.0.1:5173"):
                    raise RuntimeError("开发网页端口被未知进程占用；请先释放 5173 端口。")
                process = app.spawn("dev", [node, str(root / "node_modules/vite/bin/vite.js"), "--config", str(root / "apps/web/vite.config.ts"), str(root / "apps/web")])
                deadline = time.monotonic() + 10
                while not app.port_busy("http://127.0.0.1:5173"):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("开发网页未就绪，请检查 application-dev.log（开发网页日志）。")
                    await asyncio.sleep(.1)
                browser_origin = "http://127.0.0.1:5173"
                app.state.component("web", pid=process.pid, created_at=process.created_at, shared_with=None)
            except Exception as exc:
                app.stop_group("dev")
                app.frontend_error = str(exc)
                app.state.component("web", state="failed", phase="开发网页启动失败", error=str(exc))
            registry.write_text(json.dumps({"pid": os.getpid(), "url": browser_origin}), encoding="utf-8")
            if tray:
                tray.origin = browser_origin
        if not args.no_browser and not args.no_web:
            webbrowser.open(browser_origin + "/")
        app.enqueue(app.startup())
        app.enqueue(app.monitor())
        while not app.exit_event.is_set():
            if tray:
                tray.refresh()
            try:
                await asyncio.wait_for(app.exit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
    finally:
        await app.shutdown()
        await asyncio.sleep(.3)  # 让 SSE（服务器推送事件）把已退出状态送达已打开页面。
        if tray:
            tray.close()
        web.close()
        registry.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Voice Terminal 独立应用启动程序")
    parser.add_argument("--root", default="", help="项目根目录")
    parser.add_argument("--python", default="", help="业务 Python（程序运行环境）路径")
    parser.add_argument("--config", default="config/local.yaml", help="本机配置路径")
    parser.add_argument("--llm-provider", choices=("deepseek", "opencode_go"), default="")
    for name in ("no-worker", "no-analysis", "no-web", "no-tray", "no-browser", "dev"):
        parser.add_argument("--" + name, action="store_true")
    args = parser.parse_args()
    root = find_root(args.root)
    identity = hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24]
    mutex = SingleInstance("VoiceTerminal-" + identity)
    if not mutex.acquired:
        try:
            registry = root / ".runtime/application/instance.json"
            for _ in range(50):
                if registry.is_file():
                    if not args.no_browser:
                        webbrowser.open(json.loads(registry.read_text(encoding="utf-8"))["url"])
                    return
                time.sleep(.1)
            return
        finally:
            mutex.close()
    logdir = root / "logs"
    logdir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logdir / "application.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s", force=True)
    try:
        asyncio.run(run(args, root))
    except KeyboardInterrupt:
        pass
    finally:
        mutex.close()


if __name__ == "__main__":
    main()
