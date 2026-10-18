from __future__ import annotations

import tempfile
import asyncio
import os
import shutil
import subprocess
import unittest
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from voice_terminal.distribution import manifest
from voice_terminal.paths import atomic_json
from voice_terminal.paths import AppPaths

from voice_terminal.config import AppConfig, DshConfig
from voice_terminal.dsh.runtime import DshRuntimeManager


class DshConfigTests(unittest.TestCase):
    def test_shadow_backend_defaults_to_dsh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig.from_mapping({}, base_dir=Path(directory))
        self.assertEqual(config.shadow_agent.backend, "dsh")

    def test_runtime_paths_cannot_escape_project_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig.from_mapping({"dsh": {"source_dir": "../outside"}}, base_dir=root)
            self.assertNotEqual(config.dsh.source_dir, (root / '../outside').resolve())
            self.assertEqual(config.dsh.source_dir.name, 'dsh')

    def test_host_and_profile_are_fixed_and_port_supports_dynamic_assignment(self) -> None:
        with self.assertRaisesRegex(ValueError, "host"):
            DshConfig(host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "profile"):
            DshConfig(profile="sdk")
        self.assertEqual(DshConfig().port, 0)
        self.assertEqual(DshConfig(port=3080).port, 3080)
        for port in (-1, 65536, True):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, "端口"):
                DshConfig(port=port)


class DshRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "需要 Windows（操作系统）的目录联接")
    def test_legacy_junction_removal_keeps_physical_dependencies_and_user_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / "runtime/node_modules"
            home = root / "data/dsh"
            modules.mkdir(parents=True)
            home.mkdir(parents=True)
            (modules / "dependency.js").write_bytes(b"physical dependency")
            (home / "user-profile.yml").write_bytes(b"user configuration")
            subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "node_modules"), str(modules)], check=True, capture_output=True)
            manager = object.__new__(DshRuntimeManager)
            manager.config = DshConfig(home_dir=home)
            manager.release_runtime = root / "runtime"
            manager._remove_legacy_modules_link(modules)
            self.assertFalse((home / "node_modules").exists())
            self.assertEqual((modules / "dependency.js").read_bytes(), b"physical dependency")
            self.assertEqual((home / "user-profile.yml").read_bytes(), b"user configuration")

    @unittest.skipUnless(os.name == "nt", "需要 Windows（操作系统）的目录联接")
    def test_unknown_junction_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, external = root / "data/dsh", root / "external"
            home.mkdir(parents=True)
            external.mkdir()
            link = home / "node_modules"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], check=True, capture_output=True)
            manager = object.__new__(DshRuntimeManager)
            manager.config = DshConfig(home_dir=home)
            manager.release_runtime = None
            try:
                with self.assertRaisesRegex(RuntimeError, "不能确认"):
                    manager._remove_legacy_modules_link(root / "runtime/node_modules")
                self.assertTrue(link.is_junction())
                self.assertEqual(link.resolve(), external.resolve())
            finally:
                link.rmdir()

    def test_physical_user_dependencies_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "data/dsh"
            modules = home / "node_modules"
            modules.mkdir(parents=True)
            (modules / "custom.js").write_bytes(b"user dependency")
            manager = object.__new__(DshRuntimeManager)
            manager.config = DshConfig(home_dir=home)
            manager.release_runtime = None
            manager._remove_legacy_modules_link(None)
            self.assertEqual((modules / "custom.js").read_bytes(), b"user dependency")

    def test_official_environment_redirects_windows_home_to_dsh_workspace(self) -> None:
        """官方 Node（脚本运行时）与 Electron（桌面框架）主目录都留在 DSH_HOME（代理数据目录）。"""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "workspace" / "data" / "dsh"
            manager = object.__new__(DshRuntimeManager)
            manager.config = DshConfig(home_dir=home)
            env = {
                "USERPROFILE": r"C:\Users\unrelated",
                "HOMEDRIVE": "C:",
                "HOMEPATH": r"\Users\unrelated",
            }

            manager._prepare_official_environment(env)

            resolved_home = home.resolve()
            self.assertEqual(Path(env["USERPROFILE"]), resolved_home)
            self.assertEqual(Path(env["HOME"]), resolved_home)
            self.assertEqual(Path(env["TEMP"]), resolved_home / "tmp")
            self.assertEqual(Path(env["APPDATA"]), resolved_home / "appdata")
            self.assertEqual(Path(env["LOCALAPPDATA"]), resolved_home / "local-appdata")
            self.assertEqual(Path(env["PNPM_HOME"]), resolved_home / "pnpm" / "bin")
            self.assertEqual(Path(env["NPM_CONFIG_USERCONFIG"]), resolved_home / "config" / "npmrc")
            self.assertEqual(Path(env["NPM_CONFIG_STORE_DIR"]), resolved_home / "pnpm" / "store")
            if os.name == "nt":
                drive, tail = os.path.splitdrive(str(resolved_home))
                if not tail.startswith(("\\", "/")):
                    tail = "\\" + tail.lstrip("\\/")
                self.assertEqual(env["HOMEDRIVE"], drive)
                self.assertEqual(env["HOMEPATH"], tail)

    def test_dsh_child_uses_workspace_python_before_system_python(self) -> None:
        """PATH（搜索路径）优先使用项目解释器，兼容无全局 Python（运行环境）的分发机器。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "runtimes/python/python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"managed interpreter")
            atomic_json(root / "portable.json", {"schema": 2, "workspace_id": "python-check", "app": "app", "python": "runtimes/python/python.exe", "dsh": "runtimes/dsh", "models": "models", "data_dir": "data", "webview2": None})
            manager = object.__new__(DshRuntimeManager)
            manager.config = DshConfig(home_dir=root / "data/dsh")
            manager.paths = AppPaths(root / "app", root, root / "data")
            env = {"PATH": os.pathsep.join([str(root / "global-python"), str(root / "system-tools")])}
            manager._prepare_official_environment(env)
            self.assertEqual(Path(env["PATH"].split(os.pathsep)[0]), python.parent.resolve())
            self.assertEqual(Path(env["VOICE_PYTHON_EXECUTABLE"]), python.resolve())
            self.assertEqual(env["PYTHONUTF8"], "1")
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            python.unlink()
            with self.assertRaisesRegex(RuntimeError, "Python（运行环境）未就绪"):
                manager._prepare_official_environment({"PATH": ""})

    def test_launch_token_is_extracted_but_redacted_from_logs(self) -> None:
        line = "Open http://127.0.0.1:3081/?token=top-secret now"
        self.assertEqual(
            DshRuntimeManager.extract_launch_url(line),
            "http://127.0.0.1:3081/?token=top-secret",
        )
        redacted = DshRuntimeManager.redact_line(line)
        self.assertIn("?token=[REDACTED]", redacted)
        self.assertNotIn("top-secret", redacted)

    def test_status_never_contains_launch_url(self) -> None:
        manager = DshRuntimeManager(DshConfig())
        manager._state = "ready"
        manager._launch_url = "http://127.0.0.1:3081/?token=secret"
        rendered = repr(manager.status_payload())
        self.assertNotIn("token", rendered.lower())
        self.assertNotIn("secret", rendered)

    def test_safe_error_message_redacts_token(self) -> None:
        rendered = DshRuntimeManager._safe_message(
            RuntimeError("failed at http://127.0.0.1:3081/?token=secret-value"),
        )
        self.assertIn("token=[REDACTED]", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_profile_patch_validation_keeps_persisted_plugin_settings(self) -> None:
        """Profile validation accepts persisted settings beside managed rows（允許受管项外的持久设置）."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yml"
            managed = root / "managed.yml"
            source.write_text(
                "- id: agent-preset-registry\n  config:\n    default: shadow-agent\n"
                "\n- id: session-log-deepseek\n  disabled: true\n",
                encoding="utf-8",
            )
            managed.write_text(
                source.read_text(encoding="utf-8")
                + "\n- id: voice-terminal-shadow-bridge\n  config:\n    workspaceId: saved-workspace\n",
                encoding="utf-8",
            )
            self.assertTrue(DshRuntimeManager._profile_patch_preserves_managed_defaults(source, managed))

    def test_profile_patch_validation_requires_managed_defaults(self) -> None:
        """Profile validation rejects missing managed rows（缺失受管配置时校验失败）."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yml"
            managed = root / "managed.yml"
            source.write_text(
                "- id: agent-preset-registry\n  config:\n    default: shadow-agent\n"
                "\n- id: session-log-deepseek\n  disabled: true\n",
                encoding="utf-8",
            )
            managed.write_text("- id: unrelated\n  enabled: true\n", encoding="utf-8")
            self.assertFalse(DshRuntimeManager._profile_patch_preserves_managed_defaults(source, managed))

    def test_profile_patch_validation_rejects_retired_agent_presets_row(self) -> None:
        """旧版 agent-presets 配置行不能作为新版注册器配置（拒绝旧预设注册项）."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.yml"
            managed = root / "managed.yml"
            legacy.write_text(
                "- id: agent-presets\n  config:\n    default: shadow-agent\n"
                "\n- id: session-log-deepseek\n  disabled: true\n",
                encoding="utf-8",
            )
            managed.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertFalse(DshRuntimeManager._profile_patch_preserves_managed_defaults(legacy, managed))


class DshRuntimeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_refresh_preserves_native_task_selection_and_patch(self) -> None:
        """更新程序默认配置时保留官方会话任务选择及其用户补丁。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            config = DshConfig(source_dir=root / "source", home_dir=root / "home")
            metadata = {"schema": 1, "version": config.expected_version, "commit": config.expected_commit,
                        "bridge_protocol": config.bridge_protocol_version, "node": "node.exe",
                        "entry": "runtime-bootstrap.mjs", "schedule": "node_modules/@deepseek-ai/dsh-schedule/lib/index.js", "modules": "node_modules"}
            for name in ("node.exe", "runtime-bootstrap.mjs", "package.json", metadata["schedule"], "node_modules/@deepseek-ai/dsh/lib/bin.js"):
                file = runtime / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture", encoding="utf-8")
            atomic_json(runtime / "runtime.json", metadata)
            atomic_json(runtime / "manifest.json", manifest(runtime, kind="dsh", build="test"))
            native = "@deepseek-ai/dsh-experimental-schedule-bundle"
            template = root / "config/dsh/profile"
            template.mkdir(parents=True)
            atomic_json(template / "package.json", {
                "dependencies": {"voice-plugin": "0.1.1"},
                "dsh": {"profile": {"bundles": ["base", "voice-plugin"]}},
            })
            profile = config.home_dir / "profiles/web"
            profile.mkdir(parents=True)
            atomic_json(profile / "package.json", {
                "dependencies": {"voice-plugin": "0.1.0", "user-plugin": "1.2.3"},
                "dsh": {"profile": {"bundles": ["base", "voice-plugin", native, "user-plugin"]}},
            })
            user_patch = b"- id: schedule\n  disabled: false\n  config:\n    retention: 30\n"
            (profile / "cordis.patch.yml").write_bytes(user_patch)
            manager = DshRuntimeManager(config)
            manager.repo_root, manager.release_runtime = root, runtime
            with patch.object(manager, "_remove_legacy_modules_link"), \
                 patch.object(manager, "_profile_patch_preserves_managed_defaults", return_value=True):
                await manager._validate_release()
                await manager._validate_release()
            package = json.loads((profile / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(package["dsh"]["profile"]["bundles"], ["base", "voice-plugin", native, "user-plugin"])
            self.assertEqual(package["dependencies"], {"voice-plugin": "0.1.1", "user-plugin": "1.2.3"})
            patch_bytes = (profile / "cordis.patch.yml").read_bytes()
            self.assertTrue(patch_bytes.startswith(user_patch))
            self.assertEqual(patch_bytes.count(b"- id: plugin-manager"), 1)
            self.assertIn(b"fallbackRegistries", patch_bytes)

    async def test_release_checks_required_entries_without_development_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            runtime.mkdir()
            config = DshConfig()
            metadata = {'schema': 1, 'version': config.expected_version, 'commit': config.expected_commit,
                        'bridge_protocol': config.bridge_protocol_version, 'node': 'node.exe',
                        'entry': 'runtime-bootstrap.mjs', 'schedule': 'node_modules/@deepseek-ai/dsh-schedule/lib/index.js', 'modules': 'node_modules'}
            for name in ('node.exe', 'runtime-bootstrap.mjs', 'package.json', metadata['schedule'], 'node_modules/@deepseek-ai/dsh/lib/bin.js'):
                file = runtime / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('fixture', encoding='utf-8')
            atomic_json(runtime / 'runtime.json', metadata)
            atomic_json(runtime / 'manifest.json', manifest(runtime, kind='dsh', build='test'))
            manager = DshRuntimeManager(config)
            manager.repo_root = root
            manager.release_runtime = runtime
            with patch.object(manager, '_remove_legacy_modules_link'), \
                 patch.object(manager, '_profile_patch_preserves_managed_defaults', return_value=True), \
                 patch('voice_terminal.dsh.runtime.shutil.which', side_effect=AssertionError('发行端不得寻找开发工具')):
                node, entry = await manager._validate_release()
                self.assertEqual(Path(node), runtime / 'node.exe')
                self.assertEqual(entry, runtime / 'runtime-bootstrap.mjs')
                (runtime / 'runtime-bootstrap.mjs').write_text('tampered', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    await manager._validate_release()

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js（JavaScript 运行环境）")
    async def test_managed_node_pipe_invokes_graceful_cleanup_without_console(self) -> None:
        manager = DshRuntimeManager(DshConfig())
        root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"VOICE_APP_ORIGIN": "http://127.0.0.1:5173"}):
            process = await asyncio.create_subprocess_exec(
                shutil.which("node"), "--require", str(root / "scripts/managed-node.cjs"), "-e",
                "const timer=setInterval(()=>{},1000);process.on('SIGTERM',()=>{console.log('gracefully closed');clearInterval(timer)})",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            manager._process = process
            try:
                await asyncio.wait_for(manager.close(), timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertIn(b"gracefully closed", await process.stdout.read())
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_unknown_port_owner_is_never_attached(self) -> None:
        class OccupiedPortManager(DshRuntimeManager):
            async def _validate_installation(self):
                return "node", Path("bin.js")

            def _port_in_use(self):
                return True

        # 显式指定端口才执行既有占用拒绝路径；默认 0（系统分配）不附着首选端口。
        manager = OccupiedPortManager(DshConfig(port=3080))
        await manager.start()
        self.assertEqual(manager.status().state, "unavailable")
        self.assertIn("未知进程", manager.status().message)
        self.assertIsNone(manager.launch_url)


    @unittest.skipUnless(os.name == "nt", "需要 Windows（操作系统）的进程树回收")
    async def test_forced_windows_shutdown_waits_for_actual_child_exit(self) -> None:
        manager = object.__new__(DshRuntimeManager)
        manager.config = DshConfig(shutdown_timeout_ms=1000)
        process = Mock(returncode=None)
        waits = 0

        async def wait():
            nonlocal waits
            waits += 1
            if waits == 1:
                raise asyncio.TimeoutError
            process.returncode = 0
            return 0

        process.wait = AsyncMock(side_effect=wait)
        killer = Mock(wait=AsyncMock(return_value=0))
        manager._process = process
        manager._reader_tasks = []
        manager.bridge = Mock()
        with patch.dict(os.environ, {"VOICE_APP_ORIGIN": ""}), patch("voice_terminal.dsh.runtime.asyncio.create_subprocess_exec", AsyncMock(return_value=killer)) as spawn:
            await manager._stop_child(force=False)
        self.assertEqual(process.wait.await_count, 2)
        self.assertEqual(process.returncode, 0)
        self.assertIsNone(manager._process)
        self.assertEqual(spawn.call_args.args[:3], ("taskkill", "/PID", str(process.pid)))


if __name__ == "__main__":
    unittest.main()
