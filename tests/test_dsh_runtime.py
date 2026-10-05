from __future__ import annotations

import tempfile
import asyncio
import os
import shutil
import subprocess
import unittest
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock
from voice_terminal.distribution import manifest
from voice_terminal.paths import atomic_json

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
            with self.assertRaisesRegex(ValueError, "must remain inside"):
                AppConfig.from_mapping(
                    {"dsh": {"source_dir": "../outside"}},
                    base_dir=root,
                )

    def test_host_and_profile_are_fixed(self) -> None:
        with self.assertRaisesRegex(ValueError, "host"):
            DshConfig(host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "profile"):
            DshConfig(profile="sdk")
        with self.assertRaisesRegex(ValueError, "port"):
            DshConfig(port=3080)


class DshRuntimeTests(unittest.TestCase):
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
            with patch.object(manager, "_ensure_modules_link", AsyncMock()), \
                 patch.object(manager, "_profile_patch_preserves_managed_defaults", return_value=True):
                await manager._validate_release()
                await manager._validate_release()
            package = json.loads((profile / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(package["dsh"]["profile"]["bundles"], ["base", "voice-plugin", native, "user-plugin"])
            self.assertEqual(package["dependencies"], {"voice-plugin": "0.1.1", "user-plugin": "1.2.3"})
            self.assertEqual((profile / "cordis.patch.yml").read_bytes(), user_patch)

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
            with patch.object(manager, '_ensure_modules_link', AsyncMock()), \
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

        manager = OccupiedPortManager(DshConfig())
        await manager.start()
        self.assertEqual(manager.status().state, "unavailable")
        self.assertIn("未知进程", manager.status().message)
        self.assertIsNone(manager.launch_url)


if __name__ == "__main__":
    unittest.main()
