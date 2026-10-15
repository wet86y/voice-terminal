"""Process supervisor（进程监督器）：启动、诊断、采样与统一回收。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

from .state import ApplicationState
from .environment import resolve_python
from .windows import Job, ManagedProcess
from ..core.state import SERVICE_LABELS, SERVICE_LOADING_STATES, SERVICE_READY_STATES
from ..resources import ResourcePolicy
from .resource_readiness import inspect_model_resources, inspect_dsh_runtime
from .ports import validate_endpoint_report

log = logging.getLogger(__name__)

# PROBE_FAILURE_THRESHOLD / PROBE_GRACE_SECONDS（探测失败阈值与宽限时间）：单次健康检查抖动
# 不能判定核心能力失败；连续多次且持续超过宽限期才进入故障状态。
PROBE_FAILURE_THRESHOLD = 3
PROBE_GRACE_SECONDS = 6.0


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
        self.local_tts_pid: int | None = None  # local_tts_pid（本地 TTS 子进程编号）：单独计入语音合成组件。
        self.generations: dict[str, str] = {}
        self.manifest = {}
        self.resource_issues: dict[str, str] = {}  # resource_issues（各组件缺失本地资源的说明）。
        self._dsh_resource_checked = False  # 本次启动已完成代理组件盘点，显式恢复时重新检查。
        self._dsh_resource_issue: str | None = None
        self.voice_entry_ready = False  # voice_entry_ready（轻量语音入口已监听）。
        self.voice_local_ready = False  # voice_local_ready（第三路本地模型加载已结束）。
        self.voice_entry_only = False  # voice_entry_only（本次生成采用分阶段启动）。
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
        self.frontend_directory = root / "apps/frontend/dist"  # frontend_directory（唯一静态界面目录）：启动入口解析后供检查与重试共用。
        self.gpu_fallback_roles: set[str] = set()  # gpu_fallback_roles（显卡进程启动失败后使用中央处理器的组件）。
        self.exit_callback = None  # exit_callback（桌面外壳的退出协调回调）。
        self.shutdown_deadline = None  # shutdown_deadline（统一退出的单调时钟截止时间）。
        # 探测失败去抖：连续次数与首次失败时刻，成功即清零。
        self.probe_failure_threshold = PROBE_FAILURE_THRESHOLD
        self.probe_grace_seconds = PROBE_GRACE_SECONDS
        self._probe_failures: dict[str, int] = {}
        self._probe_failure_since: dict[str, float] = {}
        self.capability_rows: list[dict] = []  # capability_rows（语音服务上报的分能力状态）。

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

    def request_capability_retry(self, capability: str) -> bool:
        """request_capability_retry（请求能力级恢复）：只重建一个能力，不重启语音进程。"""
        row = self._capability_row(capability)
        if row is None or not row.get("retry"):
            return False
        voice = self.processes.get("voice")
        if voice is None or voice.poll() is not None:
            # 语音进程已退出：能力级恢复无从谈起，交给整进程恢复。
            return self.request_retry("voice")
        with self.state.condition:
            snapshot = self.state.snapshot()
            if snapshot["stopping"]:
                return False
            voice_row = next((c for c in snapshot["components"] if c["id"] == "voice"), None)
            if voice_row is None or voice_row["retrying"]:
                # 同一能力（或整进程）恢复期间拒绝重复请求，与组件级重试的门禁一致。
                return False
            self.state.component("voice", retrying=True, phase=f"恢复{row.get('label') or capability}")
        self.enqueue(self._retry_capability_locked(capability))
        return True

    def _capability_row(self, capability: str) -> dict | None:
        name = str(capability or "")
        return next((row for row in self.capability_rows if row.get("id") == name), None)

    def _pending_capability_row(self) -> dict | None:
        """_pending_capability_row（待恢复能力）：优先阻塞能力，其次可选能力。"""
        rows = [row for row in self.capability_rows if row.get("retry")]
        return next((row for row in rows if row.get("blocking")), rows[0] if rows else None)

    async def _retry_capability_locked(self, capability: str) -> None:
        async with self.operation_lock:
            await self.retry_capability(capability)

    async def retry_capability(self, capability: str) -> None:
        """retry_capability（能力级恢复）：进程存活时只重建该能力，调用方持有 operation_lock。"""
        if self.state.value["stopping"]:
            return
        label = (self._capability_row(capability) or {}).get("label") or capability
        voice = self.processes.get("voice")
        try:
            if voice is None or voice.poll() is not None:
                log.info("能力恢复回落到整进程重建：组件=voice，能力=%s", capability)
                await self.retry("voice")
                return
            log.info("开始能力级恢复：能力=%s", capability)
            response = await self.voice_control(
                "capability-retry", payload={"capability": capability},
                timeout=self.manifest.get("worker_timeout_seconds", 180) + 10)
            if not response.get("ok"):
                raise RuntimeError(response.get("message") or "组件恢复失败")
            await self.probe("voice")
            self._reset_probe_failure("voice")
        except Exception as exc:
            log.exception("能力级恢复失败：能力=%s", capability)
            self.state.component("voice", error=f"{label}恢复失败：{str(exc)[:160]}")
        finally:
            self.state.component("voice", retrying=False)
            self.state.finish()

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
        if not isinstance(payload, dict):
            return False
        key = payload.get("component")
        generation = payload.get("generation")
        if not isinstance(key, str) or key not in {"voice", "speaker", "dsh", "asr", "analysis"} or self.state.snapshot()["stopping"]:
            return False
        owner = "voice" if key in {"speaker", "dsh"} else key
        if generation != self.generations.get(owner):
            return False
        endpoint = payload.get("endpoint")
        endpoint_url = payload.get("endpoint_url")
        if endpoint_url is not None or endpoint is not None:
            try:
                detail = validate_endpoint_report(key, payload)
            except (TypeError, ValueError):
                return False
            if not self._endpoint_pid_is_owned(key, int(detail["pid"]), detail.get("created_at")):
                return False
            preferred = self.state.snapshot().get("endpoints", {}).get(key, {}).get("preferred_url")
            self.state.set_endpoint(key, str(detail["url"]), preferred_url=preferred,
                generation=generation, pid=int(detail["pid"]),
                created_at=detail.get("created_at"))
            if key == "asr":
                self.manifest["asr_url"] = detail["url"]
            elif key == "analysis":
                self.manifest["analysis_url"] = detail["url"]
            elif key == "voice":
                self.manifest["voice_port"] = detail["port"]
            elif key == "speaker":
                self.manifest["speaker_url"] = detail["url"]
            elif key == "dsh":
                self.manifest["dsh_url"] = detail["url"]
        row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
        self.state.component(key, phase=str(payload.get("phase", ""))[:200],
            **({"state": "starting"} if row["state"] == "waiting" else {}))
        return True

    def _endpoint_pid_is_owned(self, key: str, pid: int, created_at: float | None) -> bool:
        """Confirm a reporting PID belongs to this service generation's process tree."""
        process_key = "voice" if key in {"speaker", "dsh"} else key
        owner = self.processes.get(process_key)
        if owner is None or owner.poll() is not None:
            return False
        try:
            import psutil
            reporter = psutil.Process(pid)
            current_created_at = float(reporter.create_time())
            if created_at is not None and abs(current_created_at - created_at) > 2.0:
                return False
            if key in {"speaker", "dsh"}:
                descendants = psutil.Process(owner.pid).children(recursive=True)
                return any(child.pid == pid for child in descendants)
            return pid == owner.pid
        except Exception:
            return False

    def resolve_python(self) -> str:
        return resolve_python(self.root, self.python_option)

    def spawn(self, key: str, command: list[str]) -> ManagedProcess:
        if self.state.snapshot()["stopping"]:
            raise RuntimeError("应用正在退出")
        self.stop_group(key)
        generation = uuid.uuid4().hex
        self.generations[key] = generation
        if key in {"voice", "asr", "analysis"}:
            self.state.clear_endpoint(key, generation=generation)
        if key == "voice":
            for dependent in ("speaker", "dsh"):
                self.state.clear_endpoint(dependent, generation=generation)
        snapshot = self.state.snapshot()
        endpoint_table = {
            name: {"url": row.get("url", ""),  # url（实际端点地址）。
                "preferred_url": row.get("preferred_url", "")}  # preferred_url（首选端点地址）。
            for name, row in snapshot.get("endpoints", {}).items()
            if name in {"voice", "asr", "analysis", "speaker", "dsh"}
        }
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
            "VOICE_APP_ORIGIN": self.origin, "VOICE_APP_INTERNAL_TOKEN": self.internal_token,
            "VOICE_APP_SERVICE_GENERATION": generation,
            "VOICE_WEB_ORIGINS": self.frontend_origin or self.origin,
            "VOICE_APP_ENDPOINTS": json.dumps(endpoint_table, separators=(",", ":"))}
        if key == "dev":
            # Node（界面开发服务）只需来源、实例标识和会话转发值，不应继承内部 Bearer 凭据。
            env.pop("VOICE_APP_INTERNAL_TOKEN", None)
            env.pop("VOICE_APP_SERVICE_GENERATION", None)
            env.update(VOICE_FRONTEND_COOKIE_NAME=self.frontend_cookie_name,
                       VOICE_FRONTEND_SESSION=self.frontend_session,
                       VOICE_APP_INSTANCE_ID=self.state.value["instance_id"])
        env.update(self.paths.environment())
        if key in self.gpu_fallback_roles:
            env["VOICE_GPU_FALLBACK"] = "1"  # VOICE_GPU_FALLBACK（当前进程强制中央处理器回退）。
        env["VOICE_APP_INSTANCE_ID"] = self.state.value["instance_id"]
        env["VOICE_PROCESS_STARTED_AT"] = str(time.time())  # VOICE_PROCESS_STARTED_AT（宿主创建进程的时间）。
        env["VOICE_RESOURCE_POLICY"] = os.environ.get("VOICE_RESOURCE_POLICY", str(self.paths.config / "runtime-resources.json"))
        model_dir = self.manifest.get("model_dir")
        env = ResourcePolicy.load(Path(env["VOICE_RESOURCE_POLICY"])).environment(
            key, model_dir=Path(model_dir) if model_dir else None, parent=env)
        if self.llm_provider:
            env["VOICE_LLM_PROVIDER"] = self.llm_provider
        if key == "voice":
            env["VOICE_APP_PHASED_STARTUP"] = "1" if self.voice_entry_only else "0"
            # 缺少 DSH 时仍启动轻量 VoiceEntry（语音入口），但不解析不存在的运行包。
            env["VOICE_APP_SKIP_DSH_STARTUP"] = "1" if self.resource_issues.get("dsh") else "0"
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
        started = time.perf_counter()
        log.info("创建工作进程：组件=%s，解释器=%s，日志=%s", key, command[0], output)
        process = ManagedProcess(command, cwd=self.root, env=env,
            output=output, outer_job=self.outer_job, key=key,
            event_name=f"Local\\VoiceTerminal-{self.state.value['instance_id']}-{key}-{generation}")
        self.processes[key] = process
        log.info("工作进程已创建：组件=%s，进程编号=%s，创建耗时毫秒=%.1f",
                 key, process.pid, (time.perf_counter() - started) * 1000)
        if key not in {"bootstrap", "dev"}:
            self.state.component(key, state="starting", phase="启动进程，等待应用层就绪", error="",
                pid=process.pid, created_at=process.created_at)
        return process

    def stop_group(self, key):
        old = self.processes.pop(key, None)
        if old:
            log.info("回收工作进程：组件=%s，进程编号=%s", key, getattr(old, "pid", None))
            old.kill()
            old.close()

    async def setup(self):
        self.state.component("setup", state="starting", phase="读取有效配置与运行环境", error="")
        self.python = self.resolve_python()
        output = self.paths.logs / "application-bootstrap.log"
        output.unlink(missing_ok=True)
        from ..paths import python_command
        process = self.spawn("bootstrap", python_command(self.python, "voice_terminal.application.bootstrap", "--config", self.config, root=self.root))
        started = time.monotonic()
        timeout = 180 if self.paths.separated else 120  # 冷启动预算覆盖依赖导入与本地组件盘点。
        deadline = started + timeout
        next_progress = started + 5
        log.info("启动检查开始：进程编号=%s，超时秒数=%s，日志=%s", process.pid, timeout, output)
        while process.poll() is None:
            now = time.monotonic()
            if self.state.snapshot()["stopping"]:
                self.stop_group("bootstrap")
                raise RuntimeError("启动检查已取消：应用正在退出。")
            if now >= deadline or now >= next_progress:
                phase = self._bootstrap_phase(output)
            if now >= deadline:
                log.error("启动检查超时：进程编号=%s，耗时秒数=%.1f，最后阶段=%s", process.pid, now - started, phase)
                raise RuntimeError(f"环境与配置检查超过 {timeout} 秒，最后阶段：{phase}；请查看 application-bootstrap.log（启动检查日志）。")
            if now >= next_progress:
                self.state.component("setup", phase=phase)
                log.info("启动检查进行中：进程编号=%s，耗时秒数=%.1f，阶段=%s", process.pid, now - started, phase)
                next_progress = now + 10
            await asyncio.sleep(.1)
        if process.poll() != 0:
            phase = self._bootstrap_phase(output)
            log.error("启动检查进程退出：退出码=%s，最后阶段=%s", process.poll(), phase)
            raise RuntimeError(f"环境与配置检查失败（退出码 {process.poll()}，阶段：{phase}），请查看 application-bootstrap.log（启动检查日志）。")
        lines = output.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            self.manifest = json.loads(next(line for line in reversed(lines) if line.startswith('{"asr_url"')))
        except (StopIteration, ValueError) as exc:
            raise RuntimeError("启动检查未返回有效清单，请查看 application-bootstrap.log（启动检查日志）。") from exc
        self.stop_group("bootstrap")
        # 子进程已检查代理目录；同一次启动的模型分支只重查各自本地模型资源。
        self._dsh_resource_checked = True
        self._dsh_resource_issue = self.manifest.get("resource_issues", {}).get("dsh")
        self._refresh_resource_issues()
        log.info("启动检查完成：耗时秒数=%.1f，资源问题数量=%s", time.monotonic() - started, len(self.resource_issues))
        if self.resource_issues:
            # startup_skipped（跳过模型启动页）让用户先进入产品设置并下载或导入资源。
            self.state.update(startup_skipped=True)
        for address in (self.manifest["asr_url"], self.manifest["analysis_url"]):
            if urlsplit(address).hostname not in {"127.0.0.1", "localhost"}:
                raise RuntimeError("应用模式只管理本机回环组件；远程工作进程请使用调试入口。")
        if self.manifest["voice_host"] not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("应用模式的语音服务必须绑定本机回环地址。")
        api = f"http://127.0.0.1:{self.manifest['voice_port']}"
        self.state.set_endpoint("voice", None, preferred_url=api)
        self.state.set_endpoint("asr", None, preferred_url=self.manifest["asr_url"])
        self.state.set_endpoint("analysis", None, preferred_url=self.manifest["analysis_url"])
        self.state.set_endpoint("speaker", None, preferred_url=self.manifest.get("speaker_url", ""))
        dsh_url = self.manifest.get("dsh_url")
        if dsh_url:
            self.state.set_endpoint("dsh", None, preferred_url=dsh_url)
        else:
            # DSH（代理执行宿主）使用 --port 0 时，绑定前不发布无效端口。
            self.state.set_endpoint("dsh", None)
        self.state.component("setup", state="ready", phase="配置与运行环境已确认")
        if self.headless:
            self.state.component("web", state="ready", required=False,
                phase="无界面模式，仅接收宿主内部报告", error="")
        elif self.frontend_error:
            self.state.component("web", state="failed", phase="界面入口诊断", error=self.frontend_error)
        elif not self.dev and not (self.frontend_directory / "index.html").is_file():
            self.state.component("web", state="failed", phase="使用内置诊断页", error="正式程序界面文件不完整，请修复程序本体。")
        else:
            self.state.component("web", state="ready", phase="界面入口已就绪", error="")

    @staticmethod
    def _bootstrap_phase(output: Path) -> str:
        """读取日志末尾的阶段记录；不把配置或凭据内容带入界面错误。"""
        try:
            with output.open("rb") as stream:
                stream.seek(max(0, output.stat().st_size - 16384))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if " startup.progress " in line:
                    value = json.loads(line.split(" startup.progress ", 1)[1])
                    if isinstance(value.get("stage"), str):
                        return value["stage"][:160]
        except (OSError, ValueError, AttributeError):
            pass
        return "等待检查进程进入启动入口"

    def _refresh_resource_issues(self, *, recheck_dsh: bool = False) -> dict[str, str]:
        """Recheck fixed local files（重新检查固定本地文件）before every model start."""
        requirements = self.manifest.get("resource_requirements", {})
        if not isinstance(requirements, dict):
            requirements = {}
        requirements = {
            str(role): [item for item in resource_ids if isinstance(item, str)]
            for role, resource_ids in requirements.items()
            if isinstance(resource_ids, list)
        }
        if self.no_worker:
            requirements.pop("asr", None)
        if self.no_analysis:
            requirements.pop("analysis", None)
        issues = inspect_model_resources(
            self.root,
            Path(self.manifest.get("model_dir", self.root / ".local" / "models")),
            requirements,
        )
        if self.manifest.get("dsh_enabled"):
            if recheck_dsh or not getattr(self, "_dsh_resource_checked", False):
                from ..local_layout import read_portable
                dsh_runtime = self.paths.install_root / read_portable(self.paths.install_root)["dsh"]
                self._dsh_resource_issue = inspect_dsh_runtime(
                    self.root, dsh_runtime,
                    expected_version=str(self.manifest.get("dsh_expected_version", "")),
                    expected_commit=str(self.manifest.get("dsh_expected_commit", "")),
                    expected_bridge=str(self.manifest.get("dsh_expected_bridge", "")),
                )
                self._dsh_resource_checked = True
            if self._dsh_resource_issue:
                issues["dsh"] = self._dsh_resource_issue
        self.resource_issues = issues
        return issues

    async def http(self, url: str, *, action: str | None = None, payload: dict | None = None, timeout=3):
        def request():
            headers = {"Authorization": "Bearer " + self.internal_token}
            data = None
            if payload is not None:
                data = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            if action is not None:
                rows = {c["id"]: c for c in self.state.snapshot()["components"]}
                body = {"action": action,
                    "asr_available": rows["asr"]["state"] in {"ready", "external"},
                    "analysis_available": rows["analysis"]["state"] == "ready"}
                if action in {"initialize-session", "reinitialize"}:
                    endpoints = self.state.snapshot().get("endpoints", {})
                    body["endpoints"] = {key: row.get("url") or row.get("preferred_url")
                        for key, row in endpoints.items() if key in {"asr", "analysis", "speaker"}}
                if payload:
                    body.update(payload)  # payload（附加字段）：能力级恢复等操作需要额外参数。
                data = json.dumps(body).encode()
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

    async def voice_control(self, action, *, payload=None, timeout=190):
        return await self.http(self.state.value["voice_api_url"] + "/internal/application/control",
                               action=action, payload=payload, timeout=timeout)

    def create_wake_task(self, payload: dict) -> dict:
        """create_wake_task（手动创建任务）：仅由已校验桌面会话的网页线程调用。"""
        snapshot = self.state.snapshot()
        if self.loop is None or snapshot.get("stopping") or not snapshot.get("voice_api_url"):
            raise RuntimeError("语音业务尚未就绪，不能创建任务。")
        future = asyncio.run_coroutine_threadsafe(self.http(
            snapshot["voice_api_url"] + "/internal/application/wake-tasks",
            payload=payload, timeout=20), self.loop)
        try:
            return future.result(timeout=22)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("创建结果尚未确认，请先刷新任务列表，避免重复提交。")

    def decide_dsh_approval(self, payload: dict) -> dict:
        """decide_dsh_approval（转发权限决定）：网页已验证当前桌面会话。"""
        snapshot = self.state.snapshot()
        if self.loop is None or snapshot.get("stopping") or not snapshot.get("voice_api_url"):
            raise RuntimeError("语音业务尚未就绪，不能处理权限请求。")
        future = asyncio.run_coroutine_threadsafe(self.http(
            snapshot["voice_api_url"] + "/internal/application/dsh-approval",
            payload=payload, timeout=15), self.loop)
        try:
            return future.result(timeout=17)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("权限决定尚未确认，请等待请求状态刷新。")

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
            names = {"audio": SERVICE_LABELS["audio"], "asr": SERVICE_LABELS["asr"],
                     "llm": SERVICE_LABELS["llm"], "tts": SERVICE_LABELS["tts"]}
            if data.get("wake_enabled"):
                names["kws"] = SERVICE_LABELS["kws"]
            # 使用语音运行时的就绪契约；开始监听后的 RUNNING（运行中）以及
            # 语音合成中的 STREAMING（流式传输中）都属于正常工作状态。
            # loading（加载中）是瞬态：显示“启动中”并暂时拦截主监听，不判核心能力失败。
            loading = [label for name, label in names.items()
                       if services.get(name) in SERVICE_LOADING_STATES]
            unavailable = [label for name, label in names.items()
                           if services.get(name) not in SERVICE_READY_STATES
                           and services.get(name) not in SERVICE_LOADING_STATES]
            ready = not unavailable and not loading
            error = "核心能力未就绪：" + "、".join(unavailable) if unavailable else ""
            self.state.update(core_available=ready,
                core_phase="failed" if unavailable else "starting" if loading else "ready",
                capabilities=services, voice_api_ready=True)
            self.state.component("voice", error=error)
            if self.state.value["startup_complete"]:
                self.state.component("voice",
                    state="ready" if ready else "failed" if unavailable else "starting",
                    phase=("核心能力已就绪" if ready
                           else "核心能力不可用" if unavailable
                           else "核心能力加载中：" + "、".join(loading)),
                    error=error)
            return True
        return False

    async def start_service(self, key, *, entry_only=False, force_cpu=False):
        if key == "analysis" and (self.no_analysis or not self.manifest["analysis_enabled"]):
            self.state.component(key, state="disabled", enabled=False, phase="配置已停用")
            return
        if key == "asr" and self.no_worker:
            if not await self.probe(key):
                raise RuntimeError("外部语音识别工作进程未就绪")
            self.state.component(key, state="external", external=True, phase="使用外部进程，不负责回收")
            return
        module = {"asr": "apps.server.asr_worker", "analysis": "apps.server.analysis_worker", "voice": "apps.server.main"}[key]
        if key == "voice":
            self.voice_entry_only = entry_only
            self.voice_entry_ready = self.voice_local_ready = False
            self.state.update(voice_api_ready=False, core_available=False)
        from ..paths import python_command
        from ..gpu_environment import worker_python
        interpreter = worker_python(self.paths.install_root, self.paths.data_root or self.paths.install_root / "data", key, self.python,
                                    self.manifest.get("compute_devices", {}))
        if force_cpu:
            interpreter = self.python
            self.gpu_fallback_roles.add(key)
        else:
            self.gpu_fallback_roles.discard(key)
        try:
            process = self.spawn(key, python_command(interpreter, module, "--config", self.config, root=self.root))
        except OSError:
            if interpreter != self.python and not force_cpu:
                log.warning("显卡解释器无法建立进程，%s（组件）恢复中央处理器环境。", key)
                return await self.start_service(key, entry_only=entry_only, force_cpu=True)
            raise
        generation = self.generations[key]
        deadline = time.monotonic() + self.manifest.get("worker_timeout_seconds", 180)
        while time.monotonic() < deadline:
            if self.state.snapshot()["stopping"]:
                return
            if process.poll() is not None:
                self.state.clear_endpoint(key, generation=generation)
                if key == "voice":
                    for dependent in ("speaker", "dsh"):
                        self.state.clear_endpoint(dependent, generation=generation)
                if interpreter != self.python and not force_cpu:
                    log.warning("显卡解释器启动失败，%s（组件）恢复中央处理器环境。", key)
                    self.stop_group(key)
                    return await self.start_service(key, entry_only=entry_only, force_cpu=True)
                raise RuntimeError(f"组件启动中退出（退出码 {process.poll()}），请查看 application-{key}.log（组件启动日志）。")
            try:
                endpoint = self.state.snapshot().get("endpoints", {}).get(key, {})
                endpoint_registered = endpoint.get("generation") == generation and bool(endpoint.get("url"))
                if endpoint_registered and await self.probe(key):
                    if key == "voice" and entry_only:
                        self.state.component(key, state="waiting", phase="接口已监听，等待本地模型加载", error="")
                        self.voice_entry_ready = True
                    elif key == "voice" and not self.state.value["core_available"]:
                        self.state.component(key, state="failed", phase="接口就绪，核心能力不可用")
                    else:
                        self.state.component(key, state="ready", phase="应用层健康检查通过", error="显卡环境启动失败，当前使用中央处理器环境。" if force_cpu else "")
                    return
            except Exception:
                pass
            await asyncio.sleep(.5)
        self.stop_group(key)
        self.state.clear_endpoint(key, generation=generation)
        if key == "voice":
            for dependent in ("speaker", "dsh"):
                self.state.clear_endpoint(dependent, generation=generation)
        if interpreter != self.python and not force_cpu:
            return await self.start_service(key, entry_only=entry_only, force_cpu=True)
        raise RuntimeError("组件启动等待超时；请查看对应组件日志。")

    async def start_voice_entry(self):
        """start_voice_entry（提前建立轻量入口）：让同一所有者立即并行启动代理宿主。"""
        self.voice_entry_ready = self.voice_local_ready = False
        try:
            await self.start_service("voice", entry_only=True)
        except Exception as exc:
            self.state.component("voice", state="failed", phase="语音入口启动失败", error=str(exc))
            for key in ("speaker", "dsh"):
                self.state.component(key, state="blocked", phase="依赖语音入口", error="语音入口未就绪")

    async def start_voice_local(self):
        """start_voice_local（第三路模型加载）：复用已监听的受管语音进程。"""
        self.state.component("voice", state="starting", phase="加载关键词唤醒、语音检测与声纹", error="")
        response = await self.voice_control("initialize-local", timeout=self.manifest.get("worker_timeout_seconds", 180) + 10)
        if not response.get("ok"):
            raise RuntimeError(response.get("message", "语音本地模型加载失败"))
        self.voice_local_ready = True
        self.state.component("voice", state="starting", phase="本地模型已加载，等待识别与分析服务汇合")

    async def finish_voice_startup(self):
        """finish_voice_startup（完整就绪屏障）：依赖汇合后才建立唯一会话调度器。"""
        if not self.voice_local_ready or self.state.snapshot()["stopping"]:
            return
        try:
            response = await self.voice_control("initialize-session", timeout=self.manifest.get("worker_timeout_seconds", 180) + 10)
            await self.probe("voice")
            ready = response.get("ok") and self.state.value["core_available"]
            self.state.component("voice", state="ready" if ready else "failed",
                phase="核心能力已就绪" if ready else "接口就绪，核心能力不可用",
                error="" if ready else response.get("message", "核心能力尚未完整就绪"))
        except Exception as exc:
            self.state.update(core_available=False)
            self.state.component("voice", state="failed", phase="会话初始化失败", error=str(exc))

    async def watch_delegated_startup(self):
        """watch_delegated_startup（启动状态同步）：模型加载期间持续呈现代理与声纹进度。"""
        while not self.state.snapshot()["stopping"]:
            try:
                await self.delegated_status()
            except Exception:
                pass  # 接口短暂繁忙由最终健康检查统一判定，保留当前加载状态。
            await asyncio.sleep(.5)

    async def delegated_status(self, *, timeout=3):
        data = await self.http(self.state.value["voice_api_url"] + "/internal/application/status", timeout=timeout)
        local_tts = data.get("local_tts")
        local_tts_pid = local_tts.get("pid") if isinstance(local_tts, dict) else None
        self.local_tts_pid = (
            local_tts_pid
            if isinstance(local_tts_pid, int) and not isinstance(local_tts_pid, bool) and local_tts_pid > 0
            else None
        )
        for key in ("speaker", "dsh"):
            item = data[key]
            row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
            if row["retrying"]:
                continue
            if item.get("state") in {"failed", "disabled", "blocked", "stopped"}:
                self.state.clear_endpoint(key, generation=self.generations.get("voice"))
            self.state.component(key, **item)
        return data

    async def start_delegated(self):
        issues = self._refresh_resource_issues()
        if "voice" not in self.processes or self.processes["voice"].poll() is not None:
            for key in ("speaker", "dsh"):
                issue = issues.get(key)
                self.state.component(key, state="failed" if issue else "blocked",
                    phase="本地资源缺失" if issue else "依赖语音服务",
                    error=issue or "语音服务未运行")
            return
        data = await self.delegated_status(timeout=self.manifest.get("dsh_timeout_seconds", 120) + 10)
        if issues.get("speaker"):
            self.state.component("speaker", state="failed", phase="本地资源缺失", error=issues["speaker"], pid=None)
        if self.manifest["dsh_enabled"]:
            if issues.get("dsh"):
                self.state.component("dsh", state="failed", phase="本地资源缺失", error=issues["dsh"], pid=None)
                return
            if self.voice_entry_only:
                if not data.get("startup", {}).get("session_ready"):
                    self.state.component("dsh", state="blocked", phase="等待恢复语音会话", error="语音会话初始化未完成")
                    return
                # 内存达到 95% 时排队不计入代理实际启动超时；启动后的限时仍由原管理器执行。
                while data["dsh"]["state"] in {"waiting", "starting"}:
                    if self.state.snapshot()["stopping"]:
                        return
                    await asyncio.sleep(.5)
                    data = await self.delegated_status(timeout=self.manifest.get("dsh_timeout_seconds", 120) + 10)
                return
            self.state.component("dsh", state="starting", phase="等待代理运行环境与会话绑定")
            await self.voice_control("dsh-start", timeout=self.manifest["dsh_timeout_seconds"] + 10)
        await self.delegated_status()

    async def start_component(self, key):
        """start_component（独立组件启动）：失败不取消其他独立加载分支。"""
        if self.state.snapshot()["stopping"]:
            return
        self.state.update(phase="启动" + next(c["name"] for c in self.state.snapshot()["components"] if c["id"] == key))
        try:
            issue = self._refresh_resource_issues().get(key)
            if issue:
                self.state.component(key, state="failed", phase="本地资源缺失", error=issue, pid=None)
                return
            if key == "voice" and self.voice_entry_ready:
                await self.start_voice_local()
            else:
                await self.start_service(key)
        except Exception as exc:
            self.state.component(key, state="failed", phase="启动失败", error=str(exc), pid=None)

    def loading_usage(self, keys):
        """loading_usage（加载中已使用的内存）：校验受管进程身份，采样缺失时保留完整预算。"""
        import psutil
        result = {}
        for key in keys:
            resident, commit = 0, 0
            owned = self.processes.get(key)
            if owned:
                try:
                    process = psutil.Process(owned.pid)
                    if abs(process.create_time() - owned.created_at) < 2:
                        memory = process.memory_info()
                        resident, commit = memory.rss, getattr(memory, "private", 0) or 0
                        if key == "voice":
                            self.state.update(voice_api_ready=False)
                            # 声纹与代理子树都属于语音进程，预算只扣除实际已使用部分。
                            for child in process.children(recursive=True):
                                try:
                                    child_memory = child.memory_info()
                                    resident += child_memory.rss
                                    commit += getattr(child_memory, "private", 0) or 0
                                except psutil.Error:
                                    pass
                except (OSError, psutil.Error):
                    pass
            result[key] = (resident, commit)
        return result

    async def start_model_services(self, keys=("asr", "analysis")):
        """start_model_services（模型启动调度）：识别优先，实时预算下最多同时加载三路。"""
        from .startup_budget import parallel_admission, sample_startup_resources
        policy = ResourcePolicy.load(Path(os.environ.get("VOICE_RESOURCE_POLICY", str(self.paths.config / "runtime-resources.json"))))
        pending = list(keys)
        active = {}  # active（加载中的任务及其组件）。
        gpu_roles = set(self.manifest.get("startup_gpu_roles", ("asr", "analysis")))
        threads = dict(policy.native_threads)
        if self.manifest.get("speaker_enabled"):
            threads["voice"] = threads.get("voice", 4) + threads.get("speaker", 4)
        sampled_at = 0.0
        reason = "优先加载语音识别组件"
        try:
            while pending or active:
                if self.state.snapshot()["stopping"]:
                    return
                finished = [task for task in active if task.done()]
                for task in finished:
                    active.pop(task)
                    await task
                if pending:
                    key = pending[0]
                    # 停用组件及外部识别的健康检查不占用重型模型加载通道。
                    heavy = [role for task, role in active.items() if not task.done()
                             and not (role == "asr" and self.no_worker)]
                    enabled = key != "analysis" or not (self.no_analysis or not self.manifest.get("analysis_enabled", True))
                    allowed = not heavy or not enabled
                    now = time.monotonic()
                    if not allowed and now - sampled_at >= 1:
                        sampled_at = now
                        if os.environ.get("VOICE_STARTUP_PARALLEL") == "0":
                            reason = "已选择串行加载"
                        else:
                            resources = await asyncio.to_thread(sample_startup_resources,
                                check_gpu=key in gpu_roles and any(role in gpu_roles for role in heavy))
                            allowed, reason = parallel_admission(resources, key, self.loading_usage(heavy),
                                gpu_roles=gpu_roles, threads=threads)
                    if self.state.snapshot()["stopping"]:
                        return
                    if allowed:
                        pending.pop(0)
                        task = asyncio.create_task(self.start_component(key))
                        active[task] = key
                        log.info("启动调度：组件=%s，策略=%s", key, "并行加载" if heavy else "单路加载")
                        await asyncio.sleep(0)
                    else:
                        self.state.component(key, state="waiting", phase="等待资源：" + reason)
                if active:
                    await asyncio.wait(active, timeout=.25, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in active:
                if not task.done():
                    task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

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
            await self.start_voice_entry()
            watcher = asyncio.create_task(self.watch_delegated_startup()) if self.voice_entry_ready else None
            try:
                await self.start_model_services(("asr", "analysis", "voice") if self.voice_entry_ready else ("asr", "analysis"))
                await self.finish_voice_startup()
            finally:
                if watcher:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            if self.state.snapshot()["stopping"]:
                return
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
                issues = await asyncio.to_thread(self._refresh_resource_issues, recheck_dsh=key in {"dsh", "voice"})
                issue = issues.get(key)
                if issue:
                    raise RuntimeError(issue)
                if key == "web":
                    if self.dev:
                        raise RuntimeError("请重启桌面程序恢复开发界面；当前诊断入口仍可使用。")
                    if not (self.frontend_directory / "index.html").is_file():
                        raise RuntimeError("正式程序界面文件不完整，请修复程序本体。")
                    self.state.component("web", state="ready", phase="界面构建产物已就绪", error="")
                elif key == "setup":
                    # 已存在的组件保留所有权，重新检查后只恢复失败项。
                    await self.setup()
                    if "voice" not in self.processes:
                        await self.start_voice_entry()
                    keys = [name for name in ("asr", "analysis") if name not in self.processes]
                    if self.voice_entry_ready and not self.voice_local_ready:
                        keys.append("voice")
                    watcher = asyncio.create_task(self.watch_delegated_startup()) if self.voice_entry_ready else None
                    try:
                        await self.start_model_services(keys)
                        if self.voice_entry_ready:
                            await self.finish_voice_startup()
                    finally:
                        if watcher:
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)
                    try:
                        await self.start_delegated()
                    except Exception as exc:
                        for name in ("speaker", "dsh"):
                            row = next(c for c in self.state.snapshot()["components"] if c["id"] == name)
                            if row["state"] not in {"ready", "disabled"}:
                                self.state.component(name, state="failed", phase="组件启动失败", error=str(exc))
                elif key in {"speaker", "dsh"}:
                    self.state.clear_endpoint(key, generation=self.generations.get("voice"))
                    response = await self.voice_control(key + "-retry", timeout=self.manifest.get("dsh_timeout_seconds", 120) + 70)
                    if not response.get("ok"):
                        raise RuntimeError(response.get("message", "组件恢复失败"))
                else:
                    if key in {"asr", "analysis"} and "voice" in self.processes and self.processes["voice"].poll() is None:
                        await self.voice_control("pause")
                    if key == "voice":
                        alive = self.processes.get("voice")
                        pending = self._pending_capability_row()
                        if alive is not None and alive.poll() is None and pending is not None:
                            # 进程仍在运行：先做能力级恢复，避免整进程重启拖垮其他能力。
                            log.info("语音进程存活，改用能力级恢复：能力=%s", pending.get("id"))
                            return await self.retry_capability(str(pending.get("id")))
                    old = self.processes.get(key)
                    if old:
                        old.request_stop(f"component-retry:{key}")
                        deadline = time.monotonic() + 5
                        while old.poll() is None and time.monotonic() < deadline:
                            await asyncio.sleep(.1)
                        self.stop_group(key)
                    await self.start_service(key)
                    if key == "voice":
                        await self.start_delegated()
                    elif "voice" in self.processes and self.processes["voice"].poll() is None:
                        response = await self.voice_control("reinitialize")
                        # reinitialize 的 ok（核心就绪）还包含 LLM/TTS（大语言模型/语音合成）；
                        # 局部 ASR/Analysis（语音识别/分析）重试以实际会话初始化与 ASR 就绪为成功条件。
                        session_ready = (
                            response.get("session_initialized") is True
                            and response.get("asr_ready") is True
                        )
                        if not response.get("ok") and not session_ready:
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
        # 同一采样中的组件、进程明细和总量一起发布，避免界面混用两次测量。
        with self.state.condition:
            self._sample_metrics()

    def _sample_metrics(self):
        import psutil
        snapshot = self.state.snapshot()
        now = time.time()
        unique = {}
        process_owners = {}  # process_owners（资源进程所属组件）。
        direct_owners = {row["pid"]: row["id"] for row in reversed(snapshot["components"])
                         if row["pid"] and not row["external"]}
        local_tts_process_ids = set()
        if self.local_tts_pid:
            direct_owners[self.local_tts_pid] = "local-tts"
            try:
                local_tts_process = psutil.Process(self.local_tts_pid)
                local_tts_process_ids.add(self.local_tts_pid)
                local_tts_process_ids.update(child.pid for child in local_tts_process.children(recursive=True))
            except psutil.Error:
                pass
        sampled = {}
        complete = True  # complete（受管进程采样覆盖完整）。
        from ..gpu_memory import process_vram_bytes, process_vram_snapshot
        gpu_memory = process_vram_snapshot()

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
                    if not row["external"]:
                        complete = False
                    self.state.component(row["id"], memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                        cpu_percent=None, uptime_seconds=None, sampled_at=now)
                    continue
                identity = (pid, created)
                memory, private_commit, cpu = sample_process(process, identity)
                self.state.component(row["id"], created_at=created, uptime_seconds=max(0, now-created),
                    memory_rss_bytes=memory, memory_private_commit_bytes=private_commit,
                    gpu_dedicated_bytes=process_vram_bytes(pid, snapshot=gpu_memory),
                    cpu_percent=cpu, sampled_at=now, thread_count=process.num_threads())
                if not row["external"]:
                    unique[identity] = (memory, private_commit)
                    process_owners[identity] = direct_owners[pid]
                # 子树仅计入整体总量，不重复加入各组件行。
                if row["id"] in {"voice", "asr", "analysis", "desktop"} and not row["external"]:
                    for child in process.children(recursive=True):
                        try:
                            child_identity = (child.pid, child.create_time())
                            child_memory, child_commit, _ = sample_process(child, child_identity)
                            unique[child_identity] = (child_memory, child_commit)
                            owner = ("local-tts" if child.pid in local_tts_process_ids
                                     else direct_owners.get(child.pid, row["id"]))
                            process_owners.setdefault(child_identity, owner)
                        except psutil.Error:
                            complete = False
            except psutil.Error:
                if not row["external"]:
                    complete = False
                self.state.component(row["id"], memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                    cpu_percent=None, uptime_seconds=None, sampled_at=now)
        commits = [value[1] for value in unique.values()]
        vram = [process_vram_bytes(pid, snapshot=gpu_memory) for pid, _ in unique]
        self.state.update(sampled_at=now,
            metrics_complete=complete,  # metrics_complete（指标覆盖完整）。
            resource_processes=[{  # 所有数值来自同一去重集合；没有独立进程的检查项不占内存。
                "pid": pid, "created_at": created, "component_id": process_owners[(pid, created)],
                "memory_rss_bytes": values[0], "memory_private_commit_bytes": values[1],
                "gpu_dedicated_bytes": process_vram_bytes(pid, snapshot=gpu_memory),
            } for (pid, created), values in unique.items()],
            total_memory_rss_bytes=sum(value[0] for value in unique.values()) if complete and unique else None,
            total_gpu_dedicated_bytes=sum(vram) if complete and vram and all(v is not None for v in vram) else None,
            total_memory_private_commit_bytes=sum(commits) if complete and commits and all(value is not None for value in commits) else None)
        self.cpu_samples = {identity: value for identity, value in self.cpu_samples.items() if identity in unique}

    def _note_probe_failure(self, key: str, exc: Exception | None) -> bool:
        """_note_probe_failure（探测失败去抖）：返回本次是否已构成终态失败。

        单次健康检查抖动（事件循环繁忙、模型加载中的短暂无响应）不应把组件判死，
        否则运行期会被误报为启动失败。连续达到阈值且持续超过宽限期才返回 True。
        """
        now = time.monotonic()
        count = self._probe_failures.get(key, 0) + 1
        self._probe_failures[key] = count
        since = self._probe_failure_since.setdefault(key, now)
        elapsed = now - since
        detail = "" if exc is None else f"，错误={type(exc).__name__}: {str(exc)[:200]}"
        log.warning("组件健康检查失败：组件=%s，连续=%d次，持续=%.1f秒%s", key, count, elapsed, detail)
        if count < self.probe_failure_threshold or elapsed < self.probe_grace_seconds:
            row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
            if row["state"] not in {"failed", "blocked"}:
                self.state.component(key, state="starting", phase=f"健康检查重试中（第{count}次）", error="")
            return False
        return True

    def _reset_probe_failure(self, key: str) -> None:
        """_reset_probe_failure（探测恢复）：健康检查成功即清零去抖计数。"""
        self._probe_failures.pop(key, None)
        self._probe_failure_since.pop(key, None)

    async def monitor(self):
        while not self.state.value["stopping"]:
            if self.state.value["startup_complete"] and not self.operation_lock.locked():
                for key, process in list(self.processes.items()):
                    if key == "bootstrap":
                        continue
                    if process.poll() is not None:
                        self.stop_group(key)
                        endpoint_key = "web" if key == "dev" else key
                        endpoint_owner_generation = self.generations.get(
                            "voice" if endpoint_key in {"speaker", "dsh"} else endpoint_key)
                        self.state.clear_endpoint(endpoint_key, generation=endpoint_owner_generation)
                        self.state.component("web" if key == "dev" else key, state="failed", phase="进程已退出", error="运行中进程退出，请查看组件日志", pid=None)
                        if key in {"voice", "asr"}:
                            self.state.update(core_available=False)
                        if key == "voice":
                            for name in ("dsh", "speaker"):
                                self.state.clear_endpoint(name, generation=self.generations.get("voice"))
                                self.state.component(name, state="blocked", phase="语音服务已退出", pid=None)
                if "voice" in self.processes:
                    try:
                        await self.probe("voice")
                        self._reset_probe_failure("voice")
                    except Exception as exc:
                        if self._note_probe_failure("voice", exc):
                            self.state.update(core_available=False, core_phase="failed", voice_api_ready=False)
                            self.state.component("voice", state="failed", phase="语音接口不可用",
                                                 error="语音服务健康检查失败，请查看组件日志")
                    try:
                        await self.delegated_status()
                    except Exception as exc:
                        # 状态查询是次要调用：失败只保留上一次能力行，不能反过来判核心不可用。
                        log.info("组件状态查询失败：组件=voice，错误=%s: %s", type(exc).__name__, str(exc)[:200])
                for key in ("asr", "analysis"):
                    row = next(c for c in self.state.snapshot()["components"] if c["id"] == key)
                    if row["state"] not in {"ready", "external"}:
                        continue
                    failure: Exception | None = None
                    try:
                        healthy = await self.probe(key)
                    except Exception as exc:
                        healthy, failure = False, exc
                    if healthy:
                        self._reset_probe_failure(key)
                    elif self._note_probe_failure(key, failure):
                        self.state.component(key, state="failed", phase="应用层探测失败", error="组件连接或模型状态不可用，请重试")
                        if key == "asr":
                            self.state.update(core_available=False, core_phase="failed")
                self.state.finish()
            try:
                self.sample_metrics()
            except Exception:
                log.exception("应用进程指标采样失败")
            await asyncio.sleep(2)

    async def shutdown(self):
        # 总退出预算 15 秒，预留 1 秒发布最终状态并关闭宿主界面入口。
        deadline = min(time.monotonic() + 14, self.shutdown_deadline or float("inf"))
        self.state.update(stopping=True, state="stopping", core_available=False, voice_api_ready=False, phase="正在退出并回收组件")
        for task in tuple(self.background):
            task.cancel()
        await asyncio.gather(*tuple(self.background), return_exceptions=True)
        # 先结束 Voice（语音服务）及受委托组件，再结束模型工作进程。
        voice = self.processes.get("voice")
        if voice:
            voice.request_stop("app-exit")
            while voice.poll() is None and time.monotonic() < deadline - 3:
                await asyncio.sleep(.1)
        for process in self.processes.values():
            process.request_stop("app-exit")
        while any(p.poll() is None for p in self.processes.values()) and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        for key in tuple(self.processes):
            self.stop_group(key)
        self.outer_job.close()
        for row in self.state.snapshot()["components"]:
            self.state.component(row["id"], state="stopped", phase="已回收", pid=None)
        self.state.update(state="stopped", phase="应用已退出", core_available=False,
            total_memory_rss_bytes=None, total_memory_private_commit_bytes=None, total_gpu_dedicated_bytes=None, resource_processes=[],
            capabilities={key: "STOPPED" for key in self.state.value["capabilities"]})
