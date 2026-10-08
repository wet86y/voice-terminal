from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voice_terminal.paths import AppPaths


ROOT = Path(__file__).resolve().parents[1]
RELOCATION_SCRIPT = ROOT / "scripts" / "relocate-workspace.py"
SPEC = importlib.util.spec_from_file_location("relocate_workspace_helper", RELOCATION_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("无法载入工作区迁移辅助脚本。")
relocation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relocation)


class WorkspaceRelocationTests(unittest.TestCase):
    def test_user_keywords_stay_in_channel_data(self) -> None:
        # 用户词表属于数据，不能写入模型目录或缓存目录；两种启动渠道保持相同布局。
        for channel in ("development", "distribution"):
            paths = AppPaths(self.source / "app", self.source, self.source / "data", channel)
            self.assertEqual(paths.keywords, self.source / "data/config/kws/keywords.txt")

    def setUp(self) -> None:
        # VOICE_RELOCATION_TEST_ROOT（迁移单测根目录）由本地验收命令指向 Dtest。
        test_root = os.environ.get("VOICE_RELOCATION_TEST_ROOT")
        self.temporary = tempfile.TemporaryDirectory(
            prefix="workspace-relocation-",
            dir=test_root or None,
        )
        self.root = Path(self.temporary.name)
        self.source = self.root / "旧 工作区 中文"
        self.target = self.root / "新 工作区 中文"
        self.source.mkdir()
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_json(self, relative: str, value: object) -> Path:
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_json_and_archive_manifests_rewrite_unicode_roots_and_forward_slashes(self) -> None:
        source_forward = self.source.as_posix()
        archive_id = "0123456789abcdef0123456789abcdef"
        settings = self._write_json(
            "data/config/product-settings.json",
            {
                "voice_path": str(self.source / "voice" / "profile.json"),
                "metadata": {
                    # manifest_path（清单路径）保留上游 JSON（结构化数据格式）的正斜杠写法。
                    "manifest_path": f"{source_forward}/data/history/archives/{archive_id}/manifest.json",
                },
                "description": f"普通说明保留旧路径文本：{source_forward}/notes.txt",
                "sibling_path": f"{source_forward}-backup/untouched.json",
            },
        )
        manifest = self._write_json(
            f"data/history/archives/{archive_id}/manifest.json",
            {"source_ref": f"{source_forward}/models/sensevoice"},
        )
        expected_voice_path = str(self.target / "voice" / "profile.json")
        expected_manifest_path = f"{self.target.as_posix()}/data/history/archives/{archive_id}/manifest.json"

        result = relocation.relocate(self.source, self.target)

        settings_data = json.loads(settings.read_text(encoding="utf-8"))
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(settings_data["voice_path"], expected_voice_path)
        self.assertEqual(settings_data["metadata"]["manifest_path"], expected_manifest_path)
        self.assertEqual(settings_data["description"], f"普通说明保留旧路径文本：{self.source.as_posix()}/notes.txt")
        self.assertEqual(settings_data["sibling_path"], f"{self.source.as_posix()}-backup/untouched.json")
        self.assertEqual(manifest_data["source_ref"], f"{self.target.as_posix()}/models/sensevoice")
        self.assertIn("data/history/archives/0123456789abcdef0123456789abcdef/manifest.json", result["changed_files"])

    def test_local_yaml_rewrites_only_selected_fields_and_preserves_comments_and_text(self) -> None:
        source_forward = self.source.as_posix()
        yaml_text = (
            "# 顶层注释保留\n"
            "wake:\n"
            f'  model_dir: "{source_forward}/models/asr" # 模型目录注释\n'
            f"  keywords_file: '{source_forward}/cache/kws/keywords.txt' # 词表注释\n"
            "shadow_agent:\n"
            "  tools:\n"
            "    allowed_roots:\n"
            f'      - "{source_forward}/data/shared"\n'
            "ordinary_text: >-\n"
            f"  会话文本中的旧路径应保留：{source_forward}/notes\n"
            "other:\n"
            f"  model_dir: {source_forward}/untouched\n"
        )
        path = self.target / "data" / "config" / "local.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml_text, encoding="utf-8")

        result = relocation.relocate(self.source, self.target)

        rewritten = path.read_text(encoding="utf-8")
        self.assertIn(f'  model_dir: "{self.target.as_posix()}/models/asr" # 模型目录注释', rewritten)
        self.assertIn(f"  keywords_file: '{self.target.as_posix()}/cache/kws/keywords.txt' # 词表注释", rewritten)
        self.assertIn(f'      - "{self.target.as_posix()}/data/shared"', rewritten)
        self.assertIn("# 顶层注释保留", rewritten)
        self.assertIn("# 模型目录注释", rewritten)
        self.assertIn("# 词表注释", rewritten)
        self.assertIn(f"会话文本中的旧路径应保留：{source_forward}/notes", rewritten)
        self.assertIn(f"  model_dir: {source_forward}/untouched", rewritten)
        self.assertIn("data/config/local.yaml", result["changed_files"])

    def test_continuity_document_rewrites_only_generated_archive_links(self) -> None:
        archive_id = "abcdef0123456789abcdef0123456789"
        source_forward = self.source.as_posix()
        generated_link = f"{source_forward}/data/history/archives/{archive_id}/dialog.json"
        ordinary_reference = f"{source_forward}/data/history/archives/readme.txt"
        document = (
            f"归档链接：[{archive_id}]({generated_link})。\n"
            f"普通说明：路径 {ordinary_reference} 仍是历史会话文本。\n"
            f"其他目录：{source_forward}/data/history/archives-extra/{archive_id}/dialog.json"
        )
        path = self._write_json("data/config/continuity-state.json", {
            "document": document,
            "session_text": f"原始会话内容：{source_forward}/data/history/archives/{archive_id}/dialog.json",
        })

        result = relocation.relocate(self.source, self.target)

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn(f"({self.target.as_posix()}/data/history/archives/{archive_id}/dialog.json)", data["document"])
        self.assertIn(ordinary_reference, data["document"])
        self.assertIn(f"{source_forward}/data/history/archives-extra/{archive_id}/dialog.json", data["document"])
        self.assertIn(f"{source_forward}/data/history/archives/{archive_id}/dialog.json", data["session_text"])
        self.assertIn("data/config/continuity-state.json", result["changed_files"])

    def test_non_target_file_bytes_and_non_matching_prefix_remain_unchanged(self) -> None:
        source_forward = self.source.as_posix()
        unrelated = self.target / "data" / "config" / "user-extra.json"
        unrelated.parent.mkdir(parents=True)
        unrelated_bytes = json.dumps(
            {"source_path": f"{source_forward}/model.bin"}, ensure_ascii=False, indent=2
        ).encode("utf-8")
        unrelated.write_bytes(unrelated_bytes)
        known = self._write_json("data/config/product-settings.json", {
            "source_path": f"{source_forward}-copy/model.bin",
        })

        relocation.relocate(self.source, self.target)

        self.assertEqual(unrelated.read_bytes(), unrelated_bytes)
        self.assertEqual(json.loads(known.read_text(encoding="utf-8"))["source_path"], f"{source_forward}-copy/model.bin")

    def test_reparse_point_in_a_known_path_is_rejected(self) -> None:
        linked_target = self.target / "data" / "config" / "product-settings.json"
        linked_target.parent.mkdir(parents=True)
        linked_target.write_text("{}", encoding="utf-8")

        def marked_reparse(path: Path) -> bool:
            return path == linked_target

        with patch.object(relocation, "_is_reparse", side_effect=marked_reparse):
            with self.assertRaises(relocation.RelocationError) as error:
                relocation.relocate(self.source, self.target)
        self.assertEqual(error.exception.code, "linked_target")

    def test_app_paths_separates_development_and_distribution_data_roots(self) -> None:
        portable = {
            "schema": 2,
            "data_dir": "data",
            "app": "app",
            "python": "runtimes/python/python.exe",
            "models": "models",
            "dsh": "runtimes/dsh",
            "webview2": "runtimes/webview2",
        }
        development = self.root / "开发 项目"
        development_install = development / ".local"
        (development / "apps" / "desktop").mkdir(parents=True)
        (development / "apps" / "desktop" / "Cargo.toml").write_text("fixture", encoding="utf-8")
        development_install.mkdir()
        (development_install / "portable.json").write_text(json.dumps(portable), encoding="utf-8")

        distribution_install = self.root / "分发 安装目录"
        distribution_program = distribution_install / "app"
        distribution_program.mkdir(parents=True)
        distribution_install.mkdir(exist_ok=True)
        (distribution_install / "portable.json").write_text(json.dumps(portable), encoding="utf-8")
        development_app_program = development_install / "app"
        development_app_program.mkdir()

        development_env = {
            "VOICE_PROGRAM_ROOT": str(development),
            "VOICE_INSTALL_ROOT": str(development_install),
            "VOICE_DATA_DIR": str(development_install / "data"),
            "VOICE_LAUNCH_CHANNEL": "development",
        }
        with patch.dict(os.environ, development_env, clear=False):
            development_paths = AppPaths.resolve(development)

        distribution_env = {
            "VOICE_PROGRAM_ROOT": str(distribution_program),
            "VOICE_INSTALL_ROOT": str(distribution_install),
            "VOICE_DATA_DIR": str(distribution_install / "data"),
            "VOICE_LAUNCH_CHANNEL": "distribution",
        }
        with patch.dict(os.environ, distribution_env, clear=False):
            distribution_paths = AppPaths.resolve(distribution_program)

        # channel（启动渠道）与 data_root（用户数据根目录）必须分别属于各自的假布局。
        self.assertEqual(development_paths.channel, "development")
        self.assertEqual(development_paths.data_root, development_install / "data")
        self.assertEqual(development_paths.program_root, development)
        self.assertEqual(distribution_paths.channel, "distribution")
        self.assertEqual(distribution_paths.data_root, distribution_install / "data")
        self.assertEqual(distribution_paths.program_root, distribution_program)
        self.assertNotEqual(development_paths.config, distribution_paths.config)

        external_data = self.root / "外部 用户数据"
        source_env = {
            "VOICE_PROGRAM_ROOT": str(development),
            "VOICE_INSTALL_ROOT": str(development_install),
            "VOICE_LAUNCH_CHANNEL": "development",
        }
        with patch.dict(os.environ, source_env, clear=False):
            with self.assertRaisesRegex(ValueError, "开发用户数据只能"):
                AppPaths.resolve(development, data_dir=external_data)

        # .local/app（本机开发程序包）仍必须使用项目 .local/data（本机数据目录）。
        app_env = {
            "VOICE_PROGRAM_ROOT": str(development_app_program),
            "VOICE_INSTALL_ROOT": str(development_install),
            "VOICE_LAUNCH_CHANNEL": "development",
        }
        with patch.dict(os.environ, app_env, clear=False):
            with self.assertRaisesRegex(ValueError, "开发用户数据只能"):
                AppPaths.resolve(development_app_program, data_dir=external_data)


if __name__ == "__main__":
    unittest.main()
