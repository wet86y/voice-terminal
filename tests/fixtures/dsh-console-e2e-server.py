"""隔离的原生网页闭环夹具：真实网关、签名授权和网页协议替身，无模型或用户资料。"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext, suppress
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(str(Path(__file__).resolve().parents[2]).removeprefix("\\\\?\\"))
sys.path[:0] = [str(ROOT), str(ROOT / ".local/tools/testing")]

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, StreamingResponse

from scripts.workspace_assets import ManagedRun
from voice_terminal.devices.trust import DeviceRequestAuthenticator, DeviceTrustStore
from voice_terminal.dsh.console import DshConsoleProxy, DshConsoleTarget
from voice_terminal.server.http_api import create_app
from voice_terminal.application.ports import bind_loopback


class FixtureServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


async def listen(app):
    listener, port = bind_loopback(0)
    server = FixtureServer(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("测试监听器启动失败。")
            await asyncio.sleep(.01)
    return server, task, listener, port


async def main(directory):
    trust = DeviceTrustStore(directory)
    device_id = os.environ["VOICE_DEVICE_E2E_ID"]
    trust.approve(device_id, os.environ["VOICE_DEVICE_E2E_PUBLIC_KEY"])
    core = "a" * 32
    dsh = FastAPI()
    control = FastAPI()
    stopped = asyncio.Event()
    target = None
    real_host = None
    host_output = None
    seen = []
    payload = bytes(range(256)) * 8193

    def authenticated(request):
        expected = hashlib.sha256(request.headers["host"].encode()).hexdigest()
        return request.cookies.get("dsh-auth-e2e") == expected

    @dsh.get("/")
    async def index(request: Request):
        if request.query_params.get("token") == "fixture-only-secret":
            return Response(status_code=303, headers={"Location":"./", "Set-Cookie":
                "dsh-auth-e2e=" + hashlib.sha256(request.headers["host"].encode()).hexdigest() + "; HttpOnly; Path=/"})
        if not authenticated(request):
            return Response(status_code=401)
        return Response('<!doctype html><title>隔离 DSH 原生网页</title><script src="plugins/fixture/client.js"></script>', media_type="text/html")

    @dsh.get("/plugins/{path:path}")
    async def plugin(request: Request, path: str):
        if not authenticated(request):
            return Response(status_code=401)
        seen.append(request.scope["raw_path"].decode() + "?" + request.scope["query_string"].decode())
        return Response(b"globalThis.fixturePluginLoaded=true;", media_type="text/javascript", headers={"Set-Cookie":"dsh-auth-leak=must-not-escape; HttpOnly"})

    @dsh.post("/api/upload")
    async def upload(request: Request):
        if not authenticated(request):
            return Response(status_code=401)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
        seen.append("upload:" + hashlib.sha256(content).hexdigest())
        return Response(bytes(content), media_type="application/octet-stream")

    @dsh.get("/api/download")
    async def download(request: Request):
        if not authenticated(request):
            return Response(status_code=401)
        async def chunks():
            for offset in range(0, len(payload), 7919):
                yield payload[offset:offset + 7919]
                await asyncio.sleep(0)
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={"Content-Disposition":'attachment; filename="test.bin"'})

    @dsh.websocket("/api/remote.mux")
    async def mux(websocket: WebSocket):
        if not authenticated(websocket) or websocket.headers.get("origin") != "http://" + websocket.headers["host"]:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                content = await websocket.receive_bytes()
                await websocket.send_bytes(content)
        except Exception:
            pass

    proxy = DshConsoleProxy(lambda: target)
    runtime = SimpleNamespace(dsh=SimpleNamespace(console=proxy), core_session_id=core,
        device_trust_store=trust, device_request_authenticator=DeviceRequestAuthenticator(trust, core_session_id=core),
        _device_gateway_enabled=True, _device_gateway_port=None)
    servers = []
    try:
        row = await listen(dsh)
        servers.append(row)
        origin = f"http://127.0.0.1:{row[3]}"
        target = DshConsoleTarget(origin, "fixture-dsh-1", origin + "/?token=fixture-only-secret")
        gateway_app = create_app(runtime)
        gateway_listener, gateway_port = bind_loopback(0)
        runtime._device_gateway_port = gateway_port
        gateway_server = FixtureServer(uvicorn.Config(gateway_app, host="127.0.0.1", port=gateway_port,
            lifespan="off", access_log=False, log_level="error"))
        gateway_task = asyncio.create_task(gateway_server.serve(sockets=[gateway_listener]))
        servers.append((gateway_server, gateway_task, gateway_listener, gateway_port))
        async with asyncio.timeout(10):
            while not gateway_server.started:
                await asyncio.sleep(.01)

        @control.post("/revoke")
        async def revoke():
            trust.revoke(device_id)
            return {"revoked":True}

        @control.post("/restart")
        async def restart():
            nonlocal target
            target = DshConsoleTarget(origin, "fixture-dsh-2", origin + "/?token=fixture-only-secret")
            proxy.close_all()
            return {"restarted":True}

        @control.post("/real-host")
        async def real_dsh():
            nonlocal target, real_host, host_output
            private = directory / "real-dsh"
            private.mkdir(parents=True)
            environment = dict(os.environ)
            for name in tuple(environment):
                if name.endswith("API_KEY"):
                    environment.pop(name)
            with (directory / "real-host-stderr.log").open("wb") as error_log:
                real_host = await asyncio.create_subprocess_exec("node", str(ROOT / "tests/fixtures/dsh-console-real-host.mjs"), str(private),
                    cwd=ROOT, env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=error_log)
            async with asyncio.timeout(45):
                while True:
                    line = await real_host.stdout.readline()
                    if not line:
                        raise RuntimeError("隔离真实 DSH 未报告就绪。")
                    if line.startswith(b"VOICE_DSH_CONSOLE_HOST_READY "):
                        metadata = json.loads(line.split(b" ", 1)[1])
                        break
            async def drain_output():
                async for _line in real_host.stdout:
                    pass
            host_output = asyncio.create_task(drain_output())
            proxy.close_all()
            target = DshConsoleTarget(metadata["origin"], "isolated-real-dsh", metadata["launch_url"])
            trust.approve(device_id, os.environ["VOICE_DEVICE_E2E_PUBLIC_KEY"])
            browser_temp = directory / "browser"
            browser_temp.mkdir()
            return {"ready":True,"browser_temp":str(browser_temp)}

        @control.get("/seen")
        async def observed():
            return {"requests":seen}

        @control.post("/stop")
        async def stop():
            stopped.set()
            return {"stopped":True}

        row = await listen(control)
        servers.append(row)
        print(json.dumps({"gateway_port":gateway_port,"control_port":row[3]}), flush=True)
        await stopped.wait()
    finally:
        proxy.close_all()
        if real_host is not None:
            try:
                real_host.stdin.write(b"stop\n")
                await real_host.stdin.drain()
                real_host.stdin.close()
                await asyncio.wait_for(real_host.wait(), 10)
            except Exception:
                if real_host.returncode is None:
                    real_host.kill()
                await real_host.wait()
            if host_output is not None:
                await host_output
        for server, _, _, _ in servers:
            server.should_exit = True
        for _, task, listener, _ in servers:
            with suppress(Exception):
                await asyncio.wait_for(task, 5)
            listener.close()


if __name__ == "__main__":
    with ManagedRun("backend-suite", ROOT) as run:
        asyncio.run(main(run.path / "console"))
