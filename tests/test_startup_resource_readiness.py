from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from voice_terminal.application.resource_readiness import (
    inspect_dsh_runtime,
    inspect_model_resources,
    required_model_resources,
)
from voice_terminal.application.state import ApplicationState
from voice_terminal.application.supervisor import ApplicationHost


class StartupResourceReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        # VOICE_STARTUP_RESOURCE_TEST_ROOT（启动资源单测根目录）由验收命令限制在 Dtest。
        test_root = os.environ.get("VOICE_STARTUP_RESOURCE_TEST_ROOT")
        self.temporary = tempfile.TemporaryDirectory(
            prefix="startup-resource-readiness-",
            dir=test_root or None,
        )
        self.root = Path(self.temporary.name)
        self.program = self.root / "假程序 中文"
        self.models = self.root / "假模型 数据"
        catalog_path = self.program / "config" / "resources" / "catalog.json"
        catalog_path.parent.mkdir(parents=True)
        self.program.mkdir(exist_ok=True)
        resources = [
            self._spec("asr-demo", "演示识别模型", "asr", {"config.yaml": b"cfg", "model.pt": b"model"}),
            self._spec("analysis-demo", "演示分析模型", "analysis", {"model.onnx": b"1234"}),
        ]
        catalog_path.write_text(json.dumps({"schema_version": 1, "resources": resources}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _spec(resource_id: str, title: str, destination: str, files: dict[str, bytes]) -> dict:
        return {
            "resource_id": resource_id,
            "kind": "model",
            "title": title,
            "destination": destination,
            "version": "fixture-v1",
            "source": {"platform": "fixture"},
            "files": [
                {"path": name, "size_bytes": len(content), "sha256": "0" * 64}
                for name, content in files.items()
            ],
        }

    def test_model_readiness_reports_missing_files_without_blocking_complete_roles(self) -> None:
        asr_root = self.models / "asr"
        asr_root.mkdir(parents=True)
        (asr_root / "config.yaml").write_bytes(b"bad")  # Same size is sufficient; no hash scan occurs here.
        (asr_root / "model.pt").write_bytes(b"xxxxx")
        analysis_root = self.models / "analysis"
        analysis_root.mkdir(parents=True)
        # model.onnx（分析模型文件）故意缺失；ASR（语音识别）仍应独立就绪。

        issues = inspect_model_resources(
            self.program,
            self.models,
            {"asr": ["asr-demo"], "analysis": ["analysis-demo"]},
        )

        self.assertNotIn("asr", issues)
        self.assertIn("analysis", issues)
        self.assertIn("请下载或导入", issues["analysis"])
        self.assertIn("model.onnx", issues["analysis"])

    def test_model_readiness_reports_catalog_size_mismatch(self) -> None:
        model = self.models / "analysis" / "model.onnx"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"bad")

        issues = inspect_model_resources(self.program, self.models, {"analysis": ["analysis-demo"]})

        self.assertIn("大小不符", issues["analysis"])
        self.assertIn("预期 4 字节", issues["analysis"])

    def test_requirements_follow_the_effective_local_model_configuration(self) -> None:
        config = SimpleNamespace(
            wake=SimpleNamespace(enabled=True),
            smart_turn=SimpleNamespace(smart_turn=True, sensevoice=True),
            speaker=SimpleNamespace(mode="observe", owner_only_barge_in=False),
        )

        requirements = required_model_resources(config)

        self.assertEqual(requirements["asr"], ["paraformer-streaming"])
        self.assertEqual(requirements["voice"], ["fsmn-vad", "kws-sherpa-onnx"])
        self.assertEqual(requirements["analysis"], ["smart-turn", "sensevoice-small", "fsmn-vad"])
        self.assertEqual(requirements["speaker"], ["wespeaker-cnceleb-resnet34"])

    def test_speaker_readiness_uses_selected_optional_model(self) -> None:
        config = SimpleNamespace(
            wake=SimpleNamespace(enabled=False),
            smart_turn=SimpleNamespace(smart_turn=False, sensevoice=False),
            speaker=SimpleNamespace(mode="observe", owner_only_barge_in=False,
                                    model_id="campplus-zh-cn-common"),
        )
        self.assertEqual(required_model_resources(config)["speaker"], ["campplus-zh-cn-common"])
        config.speaker.mode = "off"
        self.assertNotIn("speaker", required_model_resources(config))
        config.speaker.owner_only_barge_in = True
        self.assertEqual(required_model_resources(config)["speaker"], ["campplus-zh-cn-common"])

    def test_dsh_readiness_requires_runtime_metadata_and_fixed_entries(self) -> None:
        runtime = self.root / "假程序 中文" / "runtimes" / "dsh"
        missing = inspect_dsh_runtime(
            self.program,
            runtime,
            expected_version="1.2.3",
            expected_commit="a" * 40,
            expected_bridge="2.0",
        )
        self.assertIn("请下载或导入", missing or "")

        runtime.mkdir(parents=True)
        metadata = {
            "schema": 1,
            "kind": "dsh",
            "source_kind": "physical_closure",
            "version": "1.2.3",
            "commit": "a" * 40,
            "bridge_protocol": "2.0",
            "node": "node.exe",
            "entry": "entry.js",
            "schedule": "schedule.js",
            "modules": "node_modules",
        }
        (runtime / "runtime.json").write_text(json.dumps(metadata), encoding="utf-8")
        files = {
            "package.json": b"{}",
            "node.exe": b"node",
            "entry.js": b"entry",
            "schedule.js": b"schedule",
            "node_modules/@deepseek-ai/dsh/lib/bin.js": b"cli",
        }
        records = [{
            "path": "runtime.json",
            "size": (runtime / "runtime.json").stat().st_size,
            "sha256": "0" * 64,
        }]
        for relative, content in files.items():
            path = runtime / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            records.append({"path": relative, "size": len(content), "sha256": "0" * 64})
        (runtime / "node_modules").mkdir(exist_ok=True)
        (runtime / "manifest.json").write_text(
            json.dumps({"schema": 1, "kind": "dsh", "files": records}), encoding="utf-8"
        )

        issue = inspect_dsh_runtime(
            self.program,
            runtime,
            expected_version="1.2.3",
            expected_commit="a" * 40,
            expected_bridge="2.0",
        )
        self.assertIsNone(issue)

    def test_supervisor_skips_model_process_when_fixed_local_files_are_missing(self) -> None:
        host = object.__new__(ApplicationHost)
        host.root = self.program
        host.manifest = {
            "model_dir": str(self.models),
            "resource_requirements": {"asr": ["asr-demo"]},
            "dsh_enabled": False,
        }
        host.no_worker = False
        host.no_analysis = False
        host.state = ApplicationState()

        with patch.object(host, "start_service", new_callable=AsyncMock) as start_service:
            asyncio.run(host.start_component("asr"))

        start_service.assert_not_awaited()
        asr = next(item for item in host.state.snapshot()["components"] if item["id"] == "asr")
        self.assertEqual(asr["state"], "failed")
        self.assertIn("请下载或导入", asr["error"])


if __name__ == "__main__":
    unittest.main()
