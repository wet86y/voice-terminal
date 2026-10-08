"""Managed service control（受管服务控制）：进度报告和无控制台退出。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.request

from .ports import validate_endpoint_url


def _process_created_at() -> float:
    """Return the OS process creation time（进程创建时间）for PID reuse checks."""
    try:
        import psutil
        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        # The report remains useful if psutil is temporarily unavailable; the
        # host still validates PID ownership against its managed process tree.
        return time.time()


def report(component: str, phase: str, **values) -> bool | None:
    origin = os.environ.get("VOICE_APP_ORIGIN")
    token = os.environ.get("VOICE_APP_INTERNAL_TOKEN")
    if not origin or not token:
        return False if "endpoint" in values or "endpoint_url" in values else None
    payload = {"component": component, "phase": phase,
        "generation": os.environ.get("VOICE_APP_SERVICE_GENERATION"), **values}
    endpoint_report = "endpoint" in values or "endpoint_url" in values
    if endpoint_report:
        try:
            request = urllib.request.Request(origin + "/internal/application/report",
                json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.load(response)
            return isinstance(result, dict) and result.get("ok") is True
        except Exception:
            return False

    def send():
        try:
            request = urllib.request.Request(origin + "/internal/application/report",
                json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=2):
                pass
        except Exception:
            pass  # 启动报告失败不改变业务执行语义。
    threading.Thread(target=send, daemon=True).start()
    return None


def report_endpoint(component: str, endpoint_url: str) -> bool:
    """Synchronously register an actual bound endpoint（实际绑定端点）."""
    origin = os.environ.get("VOICE_APP_ORIGIN")
    token = os.environ.get("VOICE_APP_INTERNAL_TOKEN")
    if not origin or not token:
        return False
    detail = validate_endpoint_url(component, endpoint_url)
    base_url = f"{detail['scheme']}://{detail['host']}:{detail['port']}"
    created_at = _process_created_at()
    payload = {"component": component, "phase": "本机端口已绑定",
        "generation": os.environ.get("VOICE_APP_SERVICE_GENERATION"),  # generation（受管服务代次）。
        "endpoint_url": detail["url"],  # endpoint_url（完整实际端点地址）。
        "pid": os.getpid(),  # pid（报告进程编号）。
        "endpoint": {
            "host": detail["host"],  # host（本机回环主机）。
            "port": detail["port"],  # port（实际监听端口）。
            "base_url": base_url,  # base_url（不含路径的服务来源）。
            "pid": os.getpid(),  # pid（拥有监听器的进程编号）。
            "created_at": created_at,  # created_at（操作系统进程创建时间）。
        }}
    request = urllib.request.Request(origin + "/internal/application/report",
        json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=3) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("应用宿主未接受本组件的实际端点登记。")
    return True


def current_endpoint_table() -> dict:
    """Read the host endpoint table（宿主端点表）using the managed token."""
    origin = os.environ.get("VOICE_APP_ORIGIN")
    token = os.environ.get("VOICE_APP_INTERNAL_TOKEN")
    if not origin or not token:
        return {}
    request = urllib.request.Request(origin + "/internal/application/endpoints",
        headers={"Authorization": "Bearer " + token, "Cache-Control": "no-store"})
    with urllib.request.urlopen(request, timeout=3) as response:
        result = json.load(response)
    endpoints = result.get("endpoints") if isinstance(result, dict) else None
    return endpoints if isinstance(endpoints, dict) else {}


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


def run_uvicorn(app, *, on_shutdown=None, sock=None, **options) -> None:
    import uvicorn
    if sock is None and not os.environ.get("VOICE_APP_SHUTDOWN_EVENT"):
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
            await server.serve(sockets=[sock] if sock is not None else None)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    asyncio.run(run())
