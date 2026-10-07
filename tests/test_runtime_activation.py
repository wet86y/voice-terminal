"""Runtime activation（业务环境激活）：双入口选择与显式回退验证。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from voice_terminal.application.environment import resolve_python


class RuntimeActivationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="资源环境测试-")
        self.root = Path(self.directory.name)
        self.executable = self.root / "业务环境/Scripts/python.exe"
        self.executable.parent.mkdir(parents=True)
        self.executable.touch()
        self.marker = self.executable.parent.parent / "voice-runtime-ready.json"
        self.marker.write_text('{"validated":true}', encoding="utf-8")
        self.profile = self.root / ".runtime/application/runtime.json"
        self.profile.parent.mkdir(parents=True)
        self.profile.write_text(json.dumps({"python": "业务环境/Scripts/python.exe"}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_relative_profile_selects_verified_environment(self):
        self.assertEqual(resolve_python(self.root), str(self.executable.resolve()))

    def test_incomplete_installation_is_not_silently_used(self):
        self.marker.write_text('{"validated":false}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "通过 --python"):
            resolve_python(self.root)

    def test_explicit_rollback_bypasses_broken_activation(self):
        self.marker.unlink()
        self.assertEqual(resolve_python(self.root, sys.executable), str(Path(sys.executable).resolve()))

    def test_unactivated_checkout_retains_original_python(self):
        self.profile.unlink()
        with patch("voice_terminal.application.environment.shutil.which", return_value=sys.executable):
            self.assertEqual(resolve_python(self.root), str(Path(sys.executable).resolve()))

    @unittest.skipUnless(os.name == "nt", "验证本机 Windows（操作系统）启动入口")
    def test_powershell_and_host_select_same_environment_without_launching(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("没有本机脚本运行环境")
        original = Path("scripts/start.ps1").read_text(encoding="utf-8-sig")
        selection = original[:original.index("$Arguments =")]
        directory = self.root / "scripts"
        directory.mkdir()
        script = directory / "select.ps1"
        script.write_text(selection + '\n[Console]::OutputEncoding = [Text.UTF8Encoding]::new()\nWrite-Output $RuntimePython\n', encoding="utf-8-sig")
        # -DataDir（业务数据目录）：开发夹具没有正式便携包，应显式指定隔离测试目录。
        result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-DataDir", str(self.root / "data")],
                                capture_output=True, encoding="utf-8-sig", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), resolve_python(self.root))
