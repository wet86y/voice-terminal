"""Process supervisor（进程监督器）：启动、诊断、采样与统一回收。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

from .state import ApplicationState
from .environment import resolve_python
from .windows import Job, ManagedProcess
from ..core.state import SERVICE_READY_STATES
from ..resources import ResourcePolicy

log = logging.getLogger(__name__)


class ApplicationHost:
    def __init__(self, root: Path, *, python: str = "", config: str = "config/local.yaml",
                 no_worker=False, no_analysis=False, llm_provider="") -> None:
        self.root, self.config = root, config
        from ..paths import AppPaths
        self.paths = AppPaths.resolve(root)
        self.python_option = python
        self.no_worker, self.no_analysis = no_worker, no_analysis
        self.llm_provider = llm_provider
        self.state = ApplicationState()
        self.internal_token = secrets.token_urlsafe(48)
        self.processes: dict[str, ManagedProcess] = {}
        self.generations: dict[str, str] = {}
        self.manifest = {}
        self.origin = ""
        self.frontend_origin = ""  # frontend_origin（受管界面来源）：界面服务对业务 API 的可信来源。
        self.frontend_cookie_name = ""  # frontend_cookie_name（实例专属会话 Cookie 名称）。
        self.frontend_session = ""  # frontend_session（仅供开发代理握手的会话值）。
        self.python = ""
        self.loop = None
        self.outer_job = Job()
        self.exit_event = asyncio.Event()
        self.operation_lock = asyncio.Lock()
        self.cpu_samples = {}
        self.background = set()
        self.dev = False
        self.headless = False  # headless（无界面模式）：保留 Host 内部报告，不要求网页构建产物。
        self.frontend_error = ""
        self.exit_callback = None  # exit_callback（桌面外壳的退出协调回调）。
        self.shutdown_deadline = None  # shutdown_deadline（统一退出的单调时钟截止时间）。

    def enqueue(self, coroutine):
        if self.loop is None:
            coroutine.close()
            return
        def schedule():
            task = asyncio.create_task(coroutine)
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        self.loop.call_soon_threadsafe(schedule)

    def request_exit(self):
        if self.exit_callback:
            self.exit_callback()
            return
        if self.loop:
            self.loop.call_soon_threadsafe(self.exit_event.set)

    def request_retry(self, key: str) -> bool:
        with self.state.condition:
            snapshot = self.state.snapshot()
            row = next((c for c in snapshot["components"] if c["id"] == key), None)
            if not row or not row["retry_allowed"]:
                return False
            self.state.component(key, retrying=True, phase="等待恢复操作")
        self.enqueue(self.retry(key))
        return True

    def report(self, payload):
        key = payload.get("component")
        generation = payload.get("generation")
        if key not in {"voice", "speaker", "dsh", "asr", "analysis"} or self.state.snapshot()["stopping"]:
            return
        owner = "voice" if key in {"speaker", "dsh"} else key
        if generation != self.generations.get(owner):
            return
        row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
        self.state.component(key, phase=str(payload.get("phase", ""))[:200],
            **({"state": "starting"} if row["state"] == "waiting" else {}))

    def resolve_python(self) -> str:
        return resolve_python(self.root, self.python_option)

    def spawn(self, key: str, command: list[str]) -> ManagedProcess:
        if self.state.snapshot()["stopping"]:
            raise RuntimeError("应用正在退出")
        self.stop_group(key)
        generation = uuid.uuid4().hex
        self.generations[key] = generation
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
            "VOICE_APP_ORIGIN": self.origin, "VOICE_APP_INTERNAL_TOKEN": self.internal_token,
            "VOICE_APP_SERVICE_GENERATION": generation,
            "VOICE_WEB_ORIGINS": self.frontend_origin or self.origin}
        if key == "dev":
            # Node（界面开发服务）只需来源、实例标识和会话转发值，不应继承内部 Bearer 凭据。
            env.pop("VOICE_APP_INTERNAL_TOKEN", None)
            env.pop("VOICE_APP_SERVICE_GENERATION", None)
            env.update(VOICE_FRONTEND_COOKIE_NAME=self.frontend_cookie_name,
                       VOICE_FRONTEND_SESSION=self.frontend_session,
                       VOICE_APP_INSTANCE_ID=self.state.value["instance_id"])
        env.update(self.paths.environment())
        env["VOICE_APP_INSTANCE_ID"] = self.state.value["instance_id"]
        env["VOICE_RESOURCE_POLICY"] = os.environ.get("VOICE_RESOURCE_POLICY", str(self.paths.config / "runtime-resources.json"))
        model_dir = self.manifest.get("model_dir")
        env = ResourcePolicy.load(Path(env["VOICE_RESOURCE_POLICY"])).environment(
            key, model_dir=Path(model_dir) if model_dir else None, parent=env)
        if self.llm_provider:
            env["VOICE_LLM_PROVIDER"] = self.llm_provider
        if key == "voice":
            rows = {c["id"]: c for c in self.state.snapshot()["components"]}
            env["VOICE_APP_ASR_AVAILABLE"] = "1" if rows["asr"]["state"] in {"ready", "external"} else "0"
            env["VOICE_APP_ANALYSIS_AVAILABLE"] = "1" if rows["analysis"]["state"] == "ready" else "0"
            if self.manifest.get("hardware_capabilities"):
                env["VOICE_HARDWARE_CAPABILITIES"] = json.dumps(self.manifest["hardware_capabilities"])
        output = self.paths.logs / f"application-{key}.log"
        if output.is_file() and output.stat().st_size > 5 * 1024 * 1024:
            for index in (3, 2, 1):
                old = output.with_name(output.name + f".{index}")
                if old.is_file():
                    if index == 3:
                        old.unlink()
                    else:
                        old.replace(output.with_name(output.name + f".{index + 1}"))
            output.replace(output.with_name(output.name + ".1"))
        process = ManagedProcess(command, cwd=self.root, env=env,
            output=output, outer_job=self.outer_job,
            event_name=f"Local\\VoiceTerminal-{self.state.value['instance_id']}-{key}-{generation}")
        self.processes[key] = process
        if key not in {"bootstrap", "dev"}:
            self.state.component(key, state="starting", phase="启动进程，等待应用层就绪", error="",
                pid=process.pid, created_at=process.created_at)
        return process

    def stop_group(self, key):
        old = self.processes.pop(key, None)
        if old:
            old.kill()
            old.close()

    async def setup(self):
        self.state.component("setup", state="starting", phase="读取有效配置与运行环境", error="")
        self.python = self.resolve_python()
        output = self.paths.logs / "application-bootstrap.log"
        output.unlink(missing_ok=True)
        from ..paths import python_command
        process = self.spawn("bootstrap", python_command(self.python, "voice_terminal.application.bootstrap", "--config", self.config, root=self.root))
        deadline = time.monotonic() + 30
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError("环境与配置检查超过 30 秒，请查看 application-bootstrap.log（启动检查日志）。")
            await asyncio.sleep(.1)
        if process.poll() != 0:
            raise RuntimeError("环境与配置检查失败，请查看 application-bootstrap.log（启动检查日志）。")
        lines = output.read_text(encoding="utf-8", errors="replace").splitlines()
        self.manifest = json.loads(next(line for line in reversed(lines) if line.startswith('{"asr_url"')))
        self.stop_group("bootstrap")
        for address in (self.manifest["asr_url"], self.manifest["analysis_url"]):
            if urlsplit(address).hostname not in {"127.0.0.1", "localhost"}:
                raise RuntimeError("应用模式只管理本机回环组件；远程工作进程请使用调试入口。")
        if self.manifest["voice_host"] not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("应用模式的语音服务必须绑定本机回环地址。")
        api = f"http://127.0.0.1:{self.manifest['voice_port']}"
        self.state.update(voice_api_url=api, voice_ws_url=api.replace("http:", "ws:") + "/ws")
        self.state.component("setup", state="ready", phase="配置与运行环境已确认")
        if self.headless:
            self.state.component("web", state="ready", required=False,
                phase="无界面模式，仅接收宿主内部报告", error="")
        elif self.frontend_error:
            self.state.component("web", state="failed", phase="界面入口诊断", error=self.frontend_error)
        elif not self.dev and not (self.root / "apps/frontend/dist/index.html").is_file():
            self.state.component("web", state="failed", phase="使用内置诊断页", error="缺少界面构建产物，请执行 npm run build（构建界面）。")
        else:
            self.state.component("web", state="ready", phase="界面入口已就绪", error="")

    @staticmethod
    def port_busy(address: str) -> bool:
        url = urlsplit(address)
        try:
            with socket.create_connection((url.hostname, url.port or 80), timeout=.3):
                return True
        except OSError:
            return False

    async def http(self, url: str, *, action: str | None = None, timeout=3):
        def request():
            headers = {"Authorization": "Bearer " + self.internal_token}
            data = None
            if action is not None:
                rows = {c["id"]: c for c in self.state.snapshot()["components"]}
                data = json.dumps({"action": action,
                    "asr_available": rows["asr"]["state"] in {"ready", "external"},
                    "analysis_available": rows["analysis"]["state"] == "ready"}).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        # urllib（HTTP 客户端）的长请求不能让 asyncio（异步运行库）在退出时等待线程池。
        # 取消恢复或启动任务后，守护线程可自行完成，宿主仍遵守 15 秒回收预算。
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def deliver(value, error):
            if not future.done():
                if error is None:
                    future.set_result(value)
                else:
                    future.set_exception(error)
        def run_request():
            value, error = None, None
            try:
                value = request()
            except Exception as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(deliver, value, error)
            except RuntimeError:
                pass  # 宿主循环已经关闭，丢弃过期回复。
        threading.Thread(target=run_request, name="application-http", daemon=True).start()
        return await future

    async def voice_control(self, action, *, timeout=190):
        return await self.http(self.state.value["voice_api_url"] + "/internal/application/control", action=action, timeout=timeout)

    async def probe(self, key):
        if key == "asr":
            import websockets
            async with websockets.connect(self.manifest["asr_url"], open_timeout=2) as connection:
                request_id = uuid.uuid4().hex
                await connection.send(json.dumps({"type": "health", "request_id": request_id}))
                data = json.loads(await asyncio.wait_for(connection.recv(), 2))
                if isinstance(data.get("hardware_capabilities"), dict):
                    self.manifest["hardware_capabilities"] = data["hardware_capabilities"]
                return data.get("status") == "READY" and data.get("model_loaded") is True and data.get("request_id") == request_id
        if key == "analysis":
            url = urlsplit(self.manifest["analysis_url"])
            data = await self.http(f"http://{url.netloc}/health")
            return all(data.get(lane, {}).get("status") == "READY" for lane in self.manifest["analysis_lanes"])
        if key == "voice":
            data = await self.http(self.state.value["voice_api_url"] + "/health")
            # HTTP readiness（接口就绪）与 core availability（核心能力可用）分别记录。
            services = data.get("services", {})
            names = {"audio": "音频设备", "asr": "ASR（语音识别）", "llm": "LLM（大语言模型）", "tts": "TTS（语音合成）"}
            if data.get("wake_enabled"):
                names["kws"] = "KWS（关键词唤醒）"
            # 使用语音运行时的就绪契约；开始监听后的 RUNNING（运行中）以及
            # 语音合成中的 STREAMING（流式传输中）都属于正常工作状态。
            unavailable = [label for name, label in names.items() if services.get(name) not in SERVICE_READY_STATES]
            ready = not unavailable
            self.state.update(core_available=ready, capabilities=services)
            error = "核心能力未就绪：" + "、".join(unavailable) if unavailable else ""
            self.state.component("voice", error=error)
            if self.state.value["startup_complete"]:
                self.state.component("voice", state="ready" if ready else "failed",
                    phase="核心能力已就绪" if ready else "核心能力不可用", error=error)
            return True
        return False

    async def start_service(self, key):
        if key == "analysis" and (self.no_analysis or not self.manifest["analysis_enabled"]):
            self.state.component(key, state="disabled", enabled=False, phase="配置已停用")
            return
        if key == "asr" and self.no_worker:
            if not await self.probe(key):
                raise RuntimeError("外部语音识别工作进程未就绪")
            self.state.component(key, state="external", external=True, phase="使用外部进程，不负责回收")
            return
        address = {"asr": self.manifest["asr_url"], "analysis": self.manifest["analysis_url"], "voice": self.state.value["voice_api_url"]}[key]
        if self.port_busy(address):
            raise RuntimeError("组件地址被未知进程占用，未接管或结束该进程：" + address)
        module = {"asr": "apps.server.asr_worker", "analysis": "apps.server.analysis_worker", "voice": "apps.server.main"}[key]
        from ..paths import python_command
        process = self.spawn(key, python_command(self.python, module, "--config", self.config, root=self.root))
        deadline = time.monotonic() + self.manifest.get("worker_timeout_seconds", 180)
        while time.monotonic() < deadline:
            if self.state.snapshot()["stopping"]:
                return
            if process.poll() is not None:
                raise RuntimeError(f"组件启动中退出（退出码 {process.poll()}），请查看 application-{key}.log（组件启动日志）。")
            try:
                if await self.probe(key):
                    if key == "voice" and not self.state.value["core_available"]:
                        self.state.component(key, state="failed", phase="接口就绪，核心能力不可用")
                    else:
                        self.state.component(key, state="ready", phase="应用层健康检查通过", error="")
                    return
            except Exception:
                pass
            await asyncio.sleep(.5)
        self.stop_group(key)
        raise RuntimeError("组件启动等待超时；请查看对应组件日志。")

    async def delegated_status(self):
        data = await self.http(self.state.value["voice_api_url"] + "/internal/application/status")
        for key in ("speaker", "dsh"):
            item = data[key]
            row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
            if row["retrying"]:
                continue
            self.state.component(key, **item)
        return data

    async def start_delegated(self):
        if "voice" not in self.processes or self.processes["voice"].poll() is not None:
            for key in ("speaker", "dsh"):
                self.state.component(key, state="blocked", phase="依赖语音服务", error="语音服务未运行")
            return
        await self.delegated_status()
        if self.manifest["dsh_enabled"]:
            self.state.component("dsh", state="starting", phase="启动代理与任务运行环境")
            await self.voice_control("dsh-start", timeout=self.manifest["dsh_timeout_seconds"] + 10)
        await self.delegated_status()

    async def startup(self):
        async with self.operation_lock:
            try:
                await self.setup()
            except Exception as exc:
                self.state.component("setup", state="failed", phase="启动检查失败", error=str(exc))
                self.stop_group("bootstrap")
                for key in ("asr", "analysis", "voice", "speaker", "dsh"):
                    self.state.component(key, state="blocked", phase="依赖启动检查", error="请先修复启动环境或配置")
                self.state.finish()
                return
            for key in ("asr", "analysis", "voice"):
                self.state.update(phase="启动" + next(c["name"] for c in self.state.value["components"] if c["id"] == key))
                try:
                    await self.start_service(key)
                except Exception as exc:
                    self.state.component(key, state="failed", phase="启动失败", error=str(exc), pid=None)
            try:
                await self.start_delegated()
            except Exception as exc:
                for key in ("speaker", "dsh"):
                    row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
                    if row["state"] not in {"ready", "disabled"}:
                        self.state.component(key, state="failed", phase="组件启动失败", error=str(exc))
            self.state.finish()

    async def retry(self, key):
        async with self.operation_lock:
            if self.state.value["stopping"]:
                return
            try:
                self.state.component(key, state="starting", error="", phase="恢复组件")
                if key == "web":
                    if self.dev:
                        raise RuntimeError("请重启桌面程序恢复开发界面；当前诊断入口仍可使用。")
                    if not (self.root / "apps/frontend/dist/index.html").is_file():
                        raise RuntimeError("缺少界面构建产物，请先执行 npm run build（构建界面）")
                    self.state.component("web", state="ready", phase="界面构建产物已就绪", error="")
                elif key == "setup":
                    # 已存在的组件保留所有权，重新检查后只恢复失败项。
                    await self.setup()
                    for name in ("asr", "analysis", "voice"):
                        if name not in self.processes:
                            try:
                                await self.start_service(name)
                            except Exception as exc:
                                self.state.component(name, state="failed", phase="启动失败", error=str(exc), pid=None)
                    try:
                        await self.start_delegated()
                    except Exception as exc:
                        for name in ("speaker", "dsh"):
                            row = next(c for c in self.state.snapshot()["components"] if c["id"] == name)
                            if row["state"] not in {"ready", "disabled"}:
                                self.state.component(name, state="failed", phase="组件启动失败", error=str(exc))
                elif key in {"speaker", "dsh"}:
                    response = await self.voice_control(key + "-retry", timeout=self.manifest.get("dsh_timeout_seconds", 120) + 70)
                    if not response.get("ok"):
                        raise RuntimeError(response.get("message", "组件恢复失败"))
                else:
                    if key in {"asr", "analysis"} and "voice" in self.processes and self.processes["voice"].poll() is None:
                        await self.voice_control("pause")
                    old = self.processes.get(key)
                    if old:
                        old.request_stop()
                        deadline = time.monotonic() + 5
                        while old.poll() is None and time.monotonic() < deadline:
                            await asyncio.sleep(.1)
                        self.stop_group(key)
                    await self.start_service(key)
                    if key == "voice":
                        await self.start_delegated()
                    elif "voice" in self.processes and self.processes["voice"].poll() is None:
                        response = await self.voice_control("reinitialize")
                        if not response.get("ok"):
                            raise RuntimeError(response.get("message", "语音业务重新连接失败"))
                        await self.probe("voice")
                        await self.delegated_status()
            except Exception as exc:
                self.stop_group("bootstrap")
                self.state.component(key, state="failed", phase="恢复失败", error=str(exc))
            finally:
                self.state.component(key, retrying=False)
                if key in {"speaker", "dsh"} and not self.state.value["stopping"]:
                    try:
                        await self.delegated_status()
                    except Exception:
                        pass
                self.state.finish()

    def sample_metrics(self):
        import psutil
        snapshot = self.state.snapshot()
        now = time.time()
        unique = {}
        sampled = {}

        def sample_process(process, identity):
            if identity not in sampled:
                memory = process.memory_info()
                # private（Windows 私有提交量）与 rss（常驻工作集）是不同口径。
                # 其他平台不以 vms（虚拟地址空间）冒充提交量，缺失时明确不可用。
                private_commit = getattr(memory, "private", None)
                times = process.cpu_times()
                cpu_time = times.user + times.system
                previous = self.cpu_samples.get(identity)
                cpu = max(0., (cpu_time - previous[1]) / (now - previous[0]) * 100) if previous and now > previous[0] else None
                self.cpu_samples[identity] = (now, cpu_time)
                sampled[identity] = (memory.rss, private_commit, cpu)
            return sampled[identity]

        for row in snapshot["components"]:
            pid = row["pid"]
            if not pid or row["state"] in {"disabled", "stopped", "blocked"}:
                continue
            try:
                process = psutil.Process(pid)
                created = process.create_time()
                if row["created_at"] and abs(created - row["created_at"]) > 2:
                    self.state.component(row["id"], memory_rss_bytes=None, memory_private_commit_bytes=None,
                        cpu_percent=None, uptime_seconds=None, sampled_at=now)
                    continue
                identity = (pid, created)
                memory, private_commit, cpu = sample_process(process, identity)
                self.state.component(row["id"], created_at=created, uptime_seconds=max(0, now-created),
                    memory_rss_bytes=memory, memory_private_commit_bytes=private_commit,
                    cpu_percent=cpu, sampled_at=now, thread_count=process.num_threads())
                if not row["external"]:
                    unique[identity] = (memory, private_commit)
                # 子树仅计入整体总量，不重复加入各组件行。
                if row["id"] in {"voice", "asr", "analysis", "desktop"} and not row["external"]:
                    for child in process.children(recursive=True):
                        try:
                            child_identity = (child.pid, child.create_time())
                            child_memory, child_commit, _ = sample_process(child, child_identity)
                            unique[child_identity] = (child_memory, child_commit)
                        except psutil.Error:
                            pass
            except psutil.Error:
                self.state.component(row["id"], memory_rss_bytes=None, memory_private_commit_bytes=None,
                    cpu_percent=None, uptime_seconds=None, sampled_at=now)
        commits = [value[1] for value in unique.values()]
        self.state.update(sampled_at=now,
            total_memory_rss_bytes=sum(value[0] for value in unique.values()) if unique else None,
            total_memory_private_commit_bytes=sum(commits) if commits and all(value is not None for value in commits) else None)
        self.cpu_samples = {identity: value for identity, value in self.cpu_samples.items() if identity in unique}

    async def monitor(self):
        while not self.state.value["stopping"]:
            if self.state.value["startup_complete"] and not self.operation_lock.locked():
                for key, process in list(self.processes.items()):
                    if key == "bootstrap":
                        continue
                    if process.poll() is not None:
                        self.stop_group(key)
                        self.state.component("web" if key == "dev" else key, state="failed", phase="进程已退出", error="运行中进程退出，请查看组件日志", pid=None)
                        if key in {"voice", "asr"}:
                            self.state.update(core_available=False)
                        if key == "voice":
                            for name in ("dsh", "speaker"):
                                self.state.component(name, state="blocked", phase="语音服务已退出", pid=None)
                if "voice" in self.processes:
                    try:
                        await self.probe("voice")
                        await self.delegated_status()
                    except Exception:
                        self.state.update(core_available=False)
                        self.state.component("voice", state="failed", phase="语音接口不可用", error="语音服务健康检查失败，请查看组件日志")
                for key in ("asr", "analysis"):
                    row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
                    if row["state"] not in {"ready", "external"}:
                        continue
                    try:
                        healthy = await self.probe(key)
                    except Exception:
                        healthy = False
                    if not healthy:
                        self.state.component(key, state="failed", phase="应用层探测失败", error="组件连接或模型状态不可用，请重试")
                        if key == "asr":
                            self.state.update(core_available=False)
                self.state.finish()
            try:
                self.sample_metrics()
            except Exception:
                log.exception("应用进程指标采样失败")
            await asyncio.sleep(2)

    async def shutdown(self):
        # 总退出预算 15 秒，预留 1 秒发布最终状态并关闭宿主界面入口。
        deadline = min(time.monotonic() + 14, self.shutdown_deadline or float("inf"))
        self.state.update(stopping=True, state="stopping", core_available=False, phase="正在退出并回收组件")
        for task in tuple(self.background):
            task.cancel()
        await asyncio.gather(*tuple(self.background), return_exceptions=True)
        # 先结束 Voice（语音服务）及受委托组件，再结束模型工作进程。
        voice = self.processes.get("voice")
        if voice:
            voice.request_stop()
            while voice.poll() is None and time.monotonic() < deadline - 3:
                await asyncio.sleep(.1)
        for process in self.processes.values():
            process.request_stop()
        while any(p.poll() is None for p in self.processes.values()) and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        for key in tuple(self.processes):
            self.stop_group(key)
        self.outer_job.close()
        for row in self.state.snapshot()["components"]:
            self.state.component(row["id"], state="stopped", phase="已回收", pid=None)
        self.state.update(state="stopped", phase="应用已退出", core_available=False,
            total_memory_rss_bytes=None, total_memory_private_commit_bytes=None,
            capabilities={key: "STOPPED" for key in self.state.value["capabilities"]})
