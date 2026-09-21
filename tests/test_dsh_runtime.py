from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


class DshRuntimeAsyncTests(unittest.IsolatedAsyncioTestCase):
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
