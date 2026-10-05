from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import DshConfig
from ..paths import AppPaths, atomic_json
from ..distribution import safe_path, verify
from .bridge import DshBridgeClient, DshBridgeHub, new_bridge_token
from .profile import merge_profile_package, migrate_schedule_patch, native_schedule_enabled

logger = logging.getLogger(__name__)

DshRuntimeState = Literal["disabled", "starting", "ready", "unavailable", "exited"]
_TOKEN_RE = re.compile(r"([?&]token=)[^\s\"'<>]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):\d+/\?token=[^\s\"'<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class DshRuntimeStatus:
    enabled: bool
    state: DshRuntimeState
    version: str | None
    profile: str
    ui_origin: str | None
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "version": self.version,
            "profile": self.profile,
            "ui_origin": self.ui_origin,
            "message": self.message,
        }


class DshRuntimeManager:
    """Own one project-local DSH Web child without exposing its launch token."""

    def __init__(self, config: DshConfig, *, callback_origin: str = "http://127.0.0.1:8765") -> None:
        self.config = config
        self.paths = AppPaths.resolve()
        self.repo_root = self.paths.program_root if self.paths.separated else config.source_dir.parents[2]
        self.release_runtime = Path(os.environ["VOICE_DSH_RUNTIME"]).resolve() if os.environ.get("VOICE_DSH_RUNTIME") else None
        self.release_record: dict = {}
        if not callback_origin.startswith("http://127.0.0.1:"):
            raise ValueError("DSH callback origin must be loopback HTTP")
        self._bridge_token = new_bridge_token()
        self.bridge = DshBridgeClient(
            origin=f"http://{config.host}:{config.port}",
            token=self._bridge_token,
            timeout_ms=config.bridge_timeout_ms,
        )
        self.bridge_hub = DshBridgeHub(self._bridge_token)
        self.callback_origin = callback_origin
        self._state: DshRuntimeState = "disabled" if not config.enabled else "unavailable"
        self._message = "" if config.enabled else "DSH 已禁用。"
        self._process: asyncio.subprocess.Process | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._monitor_task: asyncio.Task[None] | None = None
        self._launch_url: str | None = None
        self._launch_event = asyncio.Event()
        self._closing = False
        self._restart_count = 0

    @property
    def launch_url(self) -> str | None:
        return self._launch_url if self._state == "ready" else None

    def status(self) -> DshRuntimeStatus:
        return DshRuntimeStatus(
            enabled=self.config.enabled,
            state=self._state,
            version=self.config.expected_version if self.config.enabled else None,
            profile=self.config.profile,
            ui_origin=(f"http://{self.config.host}:{self.config.port}" if self.config.enabled else None),
            message=self._message,
        )

    def status_payload(self) -> dict[str, object]:
        return self.status().as_dict()

    def begin_start(self) -> None:
        if self._closing:
            return
        if not self.config.enabled:
            self._state = "disabled"
            return
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self.start(), name="dsh-web-start")

    async def wait_ready(self) -> bool:
        task = self._start_task
        if task is not None and not task.done():
            await asyncio.shield(task)
        return self._state == "ready"

    async def start(self) -> None:
        if not self.config.enabled:
            self._state = "disabled"
            return
        if not self.config.auto_start:
            self._state = "unavailable"
            self._message = "DSH 自动启动已关闭。"
            return
        if self._process is not None and self._process.returncode is None:
            return
        self._state = "starting"
        self._message = "DSH 控制台正在启动。"
        self._launch_url = None
        self._launch_event.clear()
        self._closing = False
        try:
            node, cli = await self._validate_installation()
            if self._port_in_use():
                raise RuntimeError(f"端口 {self.config.port} 已被未知进程占用；拒绝附着。")
            self.config.home_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({
                "DSH_HOME": str(self.config.home_dir),
                "DSH_PERMISSION_MODE": "read-only",
                "DSH_TELEMETRY_DISABLED": "1",
                "VOICE_DSH_BRIDGE_TOKEN": self._bridge_token,
                "VOICE_DSH_CALLBACK_ORIGIN": self.callback_origin,
                "VOICE_DSH_TASKS_PATH": str(self.config.home_dir / "storages" / "voice-terminal-wake-tasks.json"),
                "VOICE_DSH_LEGACY_WAKE_PATH": str(self.paths.config / "wake-tasks.json"),
                "VOICE_DSH_SCHEDULE_MODULE": str(safe_path(self.release_runtime, self.release_record["schedule"])) if self.release_runtime else str(self.config.source_dir / "packages" / "schedule" / "schedule" / "lib" / "index.js"),
                "VOICE_DSH_TASKS_ACTIVE": "1",
            })
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            managed = bool(os.environ.get("VOICE_APP_ORIGIN"))
            node_arguments = [node] if node else []
            if managed:
                if node:
                    node_arguments += ["--require", str(self.repo_root / "scripts/managed-node.cjs")]
                if os.name == "nt":
                    creationflags |= subprocess.CREATE_NO_WINDOW
            self._process = await asyncio.create_subprocess_exec(
                *node_arguments, str(cli), "web", "--host", self.config.host,
                "--port", str(self.config.port), "--no-open",
                cwd=str(self.repo_root), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if managed else asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
            assert self._process.stdout is not None and self._process.stderr is not None
            self._reader_tasks = [
                asyncio.create_task(self._read_stream(self._process.stdout, "stdout")),
                asyncio.create_task(self._read_stream(self._process.stderr, "stderr")),
            ]
            process = self._process
            self._monitor_task = asyncio.create_task(self._monitor(process), name="dsh-web-monitor")
            await asyncio.wait_for(
                self._wait_until_ready(process),
                timeout=self.config.startup_timeout_ms / 1000,
            )
            health = await self.bridge.health()
            if health.get("protocol_version") != self.config.bridge_protocol_version:
                raise RuntimeError("DSH Bridge 协议版本不匹配。")
            self._state = "ready"
            self._message = ""
            logger.info("DSH Web ready at %s", self.status().ui_origin)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state = "unavailable"
            self._message = self._safe_message(exc)
            logger.warning("DSH unavailable: %s", self._message)
            await self._stop_child(force=True)

    async def close(self) -> None:
        self._closing = True
        task = self._start_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_child(force=False)
        if self.config.enabled and self._state not in {"unavailable", "exited"}:
            self._state = "disabled"
            self._message = "DSH 已随 Voice Server 关闭。"

    async def _validate_installation(self) -> tuple[str, Path]:
        if self.release_runtime:
            return await self._validate_release()
        if os.name != "nt" or os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() not in {"amd64", "x86_64"}:
            raise RuntimeError("当前 DSH 基线仅正式支持 Windows x64。")
        node = os.environ.get("VOICE_NODE_EXECUTABLE") or shutil.which("node")
        if not node:
            raise RuntimeError("未找到 Node.js；需要 22.19.0 或更高兼容版本。")
        version_output = await self._run_checked(node, "--version")
        match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version_output)
        node_version = tuple(map(int, match.groups())) if match else (0, 0, 0)
        supported_node = (
            node_version[0] == 22 and node_version >= (22, 19, 0)
        ) or node_version[0] >= 24
        if not supported_node:
            raise RuntimeError(f"Node.js 版本不满足 22.19.0 最低要求（当前 {version_output.strip()}）。")
        package_file = self.config.source_dir / "package.json"
        cli = self.config.source_dir / "apps" / "cli" / "lib" / "bin.js"
        if not package_file.is_file():
            raise RuntimeError("DSH 源码缺失；请运行 scripts\\setup-dsh.ps1。")
        try:
            package_version = str(json.loads(package_file.read_text(encoding="utf-8"))["version"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("无法读取 DSH 根 package.json 版本。") from exc
        if package_version != self.config.expected_version:
            raise RuntimeError(f"DSH 源码版本不匹配：期望 {self.config.expected_version}，实际 {package_version}。")
        if not cli.is_file():
            raise RuntimeError("DSH CLI 构建产物缺失；请重新运行 scripts\\setup-dsh.ps1。")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("未找到 Git，无法验证 DSH commit。")
        head = (await self._run_checked(git, "-C", str(self.config.source_dir), "rev-parse", "HEAD")).strip()
        if head.lower() != self.config.expected_commit.lower():
            raise RuntimeError(f"DSH commit 不匹配：期望 {self.config.expected_commit}，实际 {head}。")
        cli_version = (await self._run_checked(node, str(cli), "--version", cwd=self.repo_root)).strip()
        if self.config.expected_version not in cli_version:
            raise RuntimeError(f"DSH CLI 版本不匹配：{cli_version or '无版本输出'}。")
        if self.paths.separated:
            from .profile import build_profile
            build_profile(self.repo_root, self.config.source_dir.parent / "bundles", self.config.home_dir / "profiles/web")
            await self._ensure_modules_link(self.config.source_dir / "python/sdk-runtime/node_modules")
        if not self.config.profile_patch.is_file():
            raise RuntimeError("DSH Web profile patch 缺失。")
        managed_patch = self.config.home_dir / "profiles" / "web" / "cordis.patch.yml"
        if not self._profile_patch_preserves_managed_defaults(self.config.profile_patch, managed_patch):
            raise RuntimeError("DSH_HOME 的 Web profile patch（Web 配置补丁）缺少正式 Shadow preset（Shadow 预设）或 session-log（会话日志）设置；请运行 scripts\\setup-dsh.ps1。")
        migrate_schedule_patch(managed_patch, native_enabled=native_schedule_enabled(managed_patch.parent))
        preset_bundle_dir = self.config.source_dir.parent / "bundles" / "voice-terminal-shadow-preset"
        source_preset = self.repo_root / "config" / "dsh" / "agent-presets" / "shadow-agent"
        preset_artifacts = (preset_bundle_dir / "package.json", preset_bundle_dir / "cordis.patch.yml")
        if any(not item.is_file() for item in preset_artifacts):
            raise RuntimeError("Shadow preset Bundle（Shadow 预设功能包）构建产物缺失；请运行 scripts\\setup-dsh.ps1。")
        try:
            profile_manifest = json.loads((self.config.home_dir / "profiles" / "web" / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("DSH Web profile package.json（配置清单）缺失或无效。") from exc
        preset_package_name = "@voice-terminal/dsh-shadow-agent-preset"
        wake_package_name = "@voice-terminal/dsh-wake-tasks"
        if not profile_manifest.get("dependencies", {}).get(preset_package_name):
            raise RuntimeError("Shadow preset Bundle（Shadow 预设功能包）未安装到 Web profile（网页配置）。")
        if preset_package_name not in profile_manifest.get("dsh", {}).get("profile", {}).get("bundles", []):
            raise RuntimeError("Shadow preset Bundle（Shadow 预设功能包）未注册到 Web profile（网页配置）。")
        if not profile_manifest.get("dependencies", {}).get(wake_package_name):
            raise RuntimeError("唤醒任务插件未安装到 Web profile（网页配置）。")
        if wake_package_name not in profile_manifest.get("dsh", {}).get("profile", {}).get("bundles", []):
            raise RuntimeError("唤醒任务插件未注册到 Web profile（网页配置）。")
        build_state = self.config.source_dir.parent / "build-state.json"
        try:
            state = json.loads(build_state.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("DSH build-state.json 缺失或无效。") from exc
        if state.get("schema_version") != 5:
            raise RuntimeError("DSH build-state 格式版本不受支持；请重新运行 scripts\\setup-dsh.ps1。")
        if state.get("bridge_protocol_version") != self.config.bridge_protocol_version:
            raise RuntimeError("DSH Bridge build-state 协议版本漂移；请重新运行 setup-dsh.ps1。")
        bridge_bundle_dir = self.config.source_dir.parent / "bundles" / "voice-terminal-shadow-bridge"
        bridge_source = self.repo_root / "integrations" / "dsh" / "voice-terminal-shadow-bridge"
        bridge_build_inputs = (
            bridge_source / "src",
            bridge_source / "package.json",
            bridge_source / "build.mjs",
            bridge_source / "cordis.patch.yml",
            self.repo_root / "scripts" / "build-dsh-client.mjs",
        )
        bridge_runtime_artifacts = (
            bridge_bundle_dir / "package.json",
            bridge_bundle_dir / "cordis.patch.yml",
            bridge_bundle_dir / "lib" / "index.js",
            bridge_bundle_dir / "lib" / "plugin.js",
            bridge_bundle_dir / "lib" / "settings.js",
            bridge_bundle_dir / "lib" / "settings-contract.js",
            bridge_bundle_dir / "lib" / "generated-prompt.js",
            bridge_bundle_dir / "lib" / "client.js",
        )
        wake_bundle_dir = self.config.source_dir.parent / "bundles" / "voice-terminal-wake-tasks"
        wake_source = self.repo_root / "integrations" / "dsh" / "voice-terminal-wake-tasks"
        wake_build_inputs = (
            wake_source / "src",
            wake_source / "package.json",
            wake_source / "build.mjs",
            wake_source / "cordis.patch.yml",
        )
        wake_runtime_artifacts = (
            wake_bundle_dir / "package.json",
            wake_bundle_dir / "cordis.patch.yml",
            wake_bundle_dir / "lib" / "event-id.js",
            wake_bundle_dir / "lib" / "index.js",
            wake_bundle_dir / "lib" / "task-service.js",
            wake_bundle_dir / "lib" / "plugin.js",
        )
        common_prompt = self.repo_root / "config" / "shadow_agent" / "instructions.md"
        dsh_rules = self.repo_root / "config" / "dsh" / "shadow-agent.rules.md"
        preset_runtime_artifacts = (preset_bundle_dir / "package.json", preset_bundle_dir / "cordis.patch.yml")
        checks = (
            ("bridge_runtime_artifacts_sha256", self._tree_hash(*bridge_runtime_artifacts), "Bridge 运行产物"),
            ("bridge_build_inputs_sha256", self._tree_hash(*bridge_build_inputs), "Bridge 构建输入"),
            ("shadow_preset_runtime_artifacts_sha256", self._tree_hash(*preset_runtime_artifacts), "Shadow preset Bundle 运行产物"),
            ("shadow_preset_build_inputs_sha256", self._tree_hash(source_preset), "Shadow preset Bundle 构建输入"),
            ("shadow_prompt_sha256", self._tree_hash(common_prompt, dsh_rules), "Shadow 提示词"),
            ("wake_tasks_runtime_artifacts_sha256", self._tree_hash(*wake_runtime_artifacts), "唤醒任务插件运行产物"),
            ("wake_tasks_build_inputs_sha256", self._tree_hash(*wake_build_inputs), "唤醒任务插件构建输入"),
        )
        for key, actual, label in checks:
            if state.get(key) != actual:
                raise RuntimeError(f"DSH {label} hash 漂移；请重新运行 setup-dsh.ps1。")
        try:
            bridge_package = json.loads((bridge_bundle_dir / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("DSH Bridge package.json 缺失或无效。") from exc
        if bridge_package.get("main") != "lib/plugin.js" or bridge_package.get("exports", {}).get(".") != "./lib/plugin.js":
            raise RuntimeError("DSH Bridge 插件包入口未指向带配置模式的 plugin.js。")
        try:
            wake_package = json.loads((wake_bundle_dir / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("唤醒任务插件 package.json 缺失或无效。") from exc
        if wake_package.get("main") != "lib/plugin.js" or wake_package.get("exports", {}).get(".") != "./lib/plugin.js":
            raise RuntimeError("唤醒任务插件入口未指向 plugin.js。")
        return node, cli

    async def _validate_release(self) -> tuple[str, Path]:
        """发行端校验闭合产物；不运行 Git（源码工具）或依赖安装。"""
        runtime = self.release_runtime
        assert runtime is not None
        record = json.loads((runtime / "manifest.json").read_text(encoding="utf-8-sig"))
        metadata = json.loads((runtime / "runtime.json").read_text(encoding="utf-8-sig"))
        if metadata.get("schema") != 1 or metadata.get("version") != self.config.expected_version or metadata.get("commit") != self.config.expected_commit:
            raise RuntimeError("DSH（代理执行宿主）发行产物版本与程序要求不匹配。")
        if metadata.get("bridge_protocol") != self.config.bridge_protocol_version:
            raise RuntimeError("DSH（代理执行宿主）发行产物的桥接协议不兼容。")
        node = safe_path(runtime, metadata["node"])
        cli = safe_path(runtime, metadata["entry"])
        schedule = safe_path(runtime, metadata["schedule"])
        if not all(path.is_file() for path in (node, cli, schedule)):
            raise RuntimeError("DSH（代理执行宿主）发行产物不完整。")
        # 启动检查必要入口的文件摘要，避免数万依赖文件扫描占满服务启动预算。
        # 构建、分发及手动切换组合的离线验证仍执行完整摘要和文件集合校验。
        required = {"runtime.json", "package.json", metadata["node"], metadata["entry"], metadata["schedule"],
                    "node_modules/@deepseek-ai/dsh/lib/bin.js"}
        entries = [item for item in record.get("files", []) if item.get("path") in required]
        if record.get("kind") != "dsh" or {item["path"] for item in entries} != required:
            raise RuntimeError("DSH（代理执行宿主）发布清单缺少必要入口记录。")
        await asyncio.to_thread(verify, runtime, {**record, "files": entries}, exact=False)
        self.release_record = metadata
        # 自研插件属于程序包；只补默认补丁，已迁移的用户配置继续保留。
        template = self.repo_root / "config/dsh/profile"
        profile = self.config.home_dir / "profiles/web"
        for source in template.rglob("*"):
            if not source.is_file():
                continue
            target = profile / source.relative_to(template)
            if target.name == "cordis.patch.yml" and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.relative_to(template) == Path("package.json") and target.is_file():
                managed = json.loads(source.read_text(encoding="utf-8-sig"))
                existing = json.loads(target.read_text(encoding="utf-8-sig"))
                atomic_json(target, merge_profile_package(managed, existing))
            else:
                shutil.copyfile(source, target)
        # 用户区内的受管目录联接每次启动重建目标，可随整个应用搬迁。
        # 发行运行目录自身必须没有链接；不复制开发机 pnpm（包管理器）目录联接。
        target = safe_path(runtime, metadata["modules"])
        await self._ensure_modules_link(target)
        managed_patch = profile / "cordis.patch.yml"
        if not self._profile_patch_preserves_managed_defaults(self.config.profile_patch, managed_patch):
            raise RuntimeError("DSH（代理执行宿主）的用户配置缺少必要的受管默认项。")
        migrate_schedule_patch(managed_patch, native_enabled=native_schedule_enabled(profile))
        return str(node), cli

    async def _ensure_modules_link(self, target: Path) -> None:
        target = target.resolve()
        if not target.is_dir():
            raise RuntimeError("DSH（代理执行宿主）受管依赖目录缺失。")
        link = self.config.home_dir / "node_modules"
        if link.is_junction() and link.resolve() != target:
            link.rmdir()  # 只移除本次管理的联接，不递归删除目标。
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            script = "& { param($link,$target) New-Item -ItemType Junction -Path $link -Target $target -ErrorAction Stop | Out-Null } "
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            await self._run_checked("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script + quote(link) + " " + quote(target))
        elif not link.is_junction() or link.resolve() != target:
            raise RuntimeError("DSH（代理执行宿主）用户目录的受管依赖位置存在冲突。")

    @staticmethod
    def _profile_patch_preserves_managed_defaults(source: Path, managed: Path) -> bool:
        """Allow volatile user settings（热更新用户设置） in cordis.patch.yml（Cordis 配置补丁） while retaining managed rows（受管配置项）."""
        try:
            source_text = source.read_text(encoding="utf-8-sig")
            managed_text = managed.read_text(encoding="utf-8-sig")
        except OSError:
            return False

        def entry_block(text: str, entry_id: str) -> str | None:
            match = re.search(
                rf"(?ms)^- id: {re.escape(entry_id)}\s*$\n(?P<body>.*?)(?=^- id:|\Z)",
                text,
            )
            return match.group("body") if match else None

        checks = (
            ("agent-preset-registry", re.compile(r"(?m)^\s+default:\s*shadow-agent\s*$")),
            ("session-log-deepseek", re.compile(r"(?m)^\s+disabled:\s*true\s*$")),
        )
        for entry_id, expected in checks:
            source_block = entry_block(source_text, entry_id)
            managed_block = entry_block(managed_text, entry_id)
            if source_block is None or managed_block is None or not expected.search(source_block) or not expected.search(managed_block):
                return False
        return True

    def _tree_hash(self, *paths: Path) -> str:
        rows: list[str] = []
        for root in paths:
            files = [root] if root.is_file() else sorted(
                (item for item in root.rglob("*") if item.is_file()),
                key=lambda item: str(item).lower(),
            )
            for item in files:
                relative = item.resolve().relative_to(self.repo_root.resolve()).as_posix()
                file_hash = hashlib.sha256(item.read_bytes()).hexdigest().upper()
                rows.append(f"/{relative}:{file_hash}")
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    async def _run_checked(self, *args: str, cwd: Path | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" and os.environ.get("VOICE_APP_ORIGIN") else 0,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = self.redact_line(stderr.decode(errors="replace")).strip()[:240]
            raise RuntimeError(detail or f"命令验证失败（exit {process.returncode}）。")
        return stdout.decode(errors="replace")

    async def _read_stream(self, stream: asyncio.StreamReader, name: str) -> None:
        while line := await stream.readline():
            decoded = line.decode(errors="replace").rstrip()
            launch = self.extract_launch_url(decoded)
            if launch:
                self._launch_url = launch
                self._launch_event.set()
            safe = self.redact_line(decoded)
            if safe:
                logger.info("DSH[%s] %s", name, safe)

    async def _wait_until_ready(self, process: asyncio.subprocess.Process) -> None:
        await self._launch_event.wait()
        for _ in range(100):
            if process.returncode is not None:
                raise RuntimeError(f"DSH 在就绪前退出（exit {process.returncode}）。")
            if self._port_in_use():
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("DSH 已报告启动地址，但端口未开始监听。")

    async def _monitor(self, process: asyncio.subprocess.Process) -> None:
        returncode = await process.wait()
        if process is self._process and not self._closing:
            self._state = "exited"
            self._message = f"DSH 进程已退出（exit {returncode}）。"
            self._launch_url = None
            logger.warning("%s", self._message)
            self._process = None
            readers, self._reader_tasks = self._reader_tasks, []
            await asyncio.gather(*readers, return_exceptions=True)
            if self._restart_count < self.config.restart_limit:
                self._restart_count += 1
                self._start_task = asyncio.create_task(
                    self._restart_after_exit(), name=f"dsh-web-restart-{self._restart_count}",
                )
            else:
                self._state = "unavailable"
                self._message = "DSH 已达到自动重启上限；Voice 主链继续运行。"

    async def _restart_after_exit(self) -> None:
        await asyncio.sleep(self.config.restart_delay_ms / 1000)
        if not self._closing:
            await self.start()

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.15)
            return sock.connect_ex((self.config.host, self.config.port)) == 0

    async def _stop_child(self, *, force: bool) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                if not force and os.environ.get("VOICE_APP_ORIGIN") and process.stdin:
                    process.stdin.write(b"voice-terminal:shutdown\n")
                    await process.stdin.drain()
                elif os.name == "nt" and not force:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                await asyncio.wait_for(process.wait(), self.config.shutdown_timeout_ms / 1000)
            except (OSError, asyncio.TimeoutError):
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    await killer.wait()
                elif process.returncode is None:
                    process.kill()
                    await process.wait()
        for task in self._reader_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._reader_tasks, return_exceptions=True)
        self._reader_tasks.clear()
        self._process = None
        self._launch_url = None

    @staticmethod
    def extract_launch_url(line: str) -> str | None:
        match = _URL_RE.search(line)
        return match.group(0) if match else None

    @staticmethod
    def redact_line(line: str) -> str:
        return _TOKEN_RE.sub(r"\1[REDACTED]", line)

    @classmethod
    def _safe_message(cls, exc: Exception) -> str:
        return " ".join(cls.redact_line(str(exc)).split())[:320] or type(exc).__name__
