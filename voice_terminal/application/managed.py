"""Managed service control（受管服务控制）：进度报告和无控制台退出。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.request


def report(component: str, phase: str, **values) -> None:
    origin = os.environ.get("VOICE_APP_ORIGIN")
    token = os.environ.get("VOICE_APP_INTERNAL_TOKEN")
    if not origin or not token:
        return
    payload = {"component": component, "phase": phase,
        "generation": os.environ.get("VOICE_APP_SERVICE_GENERATION"), **values}
    def send():
        try:
            request = urllib.request.Request(origin + "/internal/application/report",
                json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=2):
                pass
        except Exception:
            pass  # 启动报告失败不改变业务执行语义。
    threading.Thread(target=send, daemon=True).start()


async def wait_for_shutdown() -> None:
    name = os.environ.get("VOICE_APP_SHUTDOWN_EVENT")
    if not name:
        await asyncio.Future()
        return
    from .windows import ShutdownEvent
    event = ShutdownEvent(name, existing=True)
    try:
        while not event.signaled():
            await asyncio.sleep(.1)
    finally:
        event.close()


def run_uvicorn(app, *, on_shutdown=None, **options) -> None:
    import uvicorn
    if not os.environ.get("VOICE_APP_SHUTDOWN_EVENT"):
        uvicorn.run(app, **options)
        return
    async def run():
        server = uvicorn.Server(uvicorn.Config(app, **options))
        async def stop():
            await wait_for_shutdown()
            if on_shutdown is not None:
                on_shutdown()
            server.should_exit = True
        watcher = asyncio.create_task(stop())
        try:
            await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    asyncio.run(run())
