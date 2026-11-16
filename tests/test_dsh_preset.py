from __future__ import annotations

import unittest
from pathlib import Path
import shutil
import tempfile

import yaml


class DshPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.preset_dir = self.root / "integrations/dsh/voice-terminal/preset/shadow-agent"

    def test_preset_keeps_complete_default_tool_surface_and_subagents(self) -> None:
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node))
        rows = yaml.load(
            (self.preset_dir / "agent.cordis.yml").read_text(encoding="utf-8"),
            Loader=Loader,
        )
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["persona"]["config"]["complete"])
        for required in (
            "tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-jobs",
            "tool-skill", "tool-goal", "compaction", "tool-web", "tool-ask-user",
        ):
            self.assertIn(required, by_id)
        delegated = by_id["delegation"]["config"]
        delegated_ids = {row["id"] for row in delegated}
        self.assertIn("tool-subagent", delegated_ids)
        self.assertIn("tool-subagent-control", delegated_ids)
        self.assertIn("workflow-ptc", delegated_ids)
        self.assertNotIn("workflow-worker-thread", delegated_ids)
        self.assertNotIn("restrict", (self.preset_dir / "agent.cordis.yml").read_text(encoding="utf-8").lower())

    def test_bridge_bundle_and_managed_prompt_rules_exist(self) -> None:
        import json
        bundle = self.root / "integrations/dsh/voice-terminal"
        manifest = json.loads((bundle / "package.json").read_text(encoding="utf-8"))
        declaration = json.loads((bundle / "components.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "@voice-terminal/dsh-plugin")
        self.assertEqual(manifest["exports"]["."], "./lib/bridge/plugin.js")
        self.assertEqual(manifest["exports"]["./wake-tasks"], "./lib/wake-tasks/plugin.js")
        self.assertEqual(manifest["dsh"]["client"]["platform"], "web")
        self.assertEqual({component["id"] for component in declaration["components"]}, {"bridge", "wake-tasks", "client", "preset", "shared", "device-control"})
        rules = (self.root / "config/dsh/shadow-agent.rules.md").read_text(encoding="utf-8")
        self.assertIn("子代理", rules)
        self.assertIn("普通 assistant 正文", rules)

    def test_built_desktop_profile_contains_the_packaged_native_helper(self) -> None:
        import json
        from voice_terminal.dsh.profile import build_profile

        bundle = self.root / "integrations/dsh/voice-terminal"
        manifest = json.loads((bundle / "package.json").read_text(encoding="utf-8"))
        self.assertIn("native/win32/device-helper.ps1", manifest["files"])
        helper_source = bundle / "native/win32/device-helper.ps1"

        with tempfile.TemporaryDirectory(prefix="dsh-profile-helper-") as temporary:
            root = Path(temporary)
            program = root / "program"
            source_bundle = root / "bundles/voice-terminal"
            destination = program / "config/dsh/profile"
            source_bundle.mkdir(parents=True)
            (program / "config/dsh").mkdir(parents=True)
            shutil.copy2(bundle / "package.json", source_bundle / "package.json")
            target_helper = source_bundle / "native/win32/device-helper.ps1"
            target_helper.parent.mkdir(parents=True)
            shutil.copy2(helper_source, target_helper)
            shutil.copy2(
                self.root / "config/dsh/web-profile.patch.yml",
                program / "config/dsh/web-profile.patch.yml",
            )

            build_profile(program, root / "bundles", destination)

            installed_helper = destination / "node_modules/@voice-terminal/dsh-plugin/native/win32/device-helper.ps1"
            self.assertEqual(installed_helper.read_bytes(), helper_source.read_bytes())

    def test_web_patch_selects_shadow_and_disables_session_upload(self) -> None:
        patch = yaml.safe_load((self.root / "config" / "dsh" / "web-profile.patch.yml").read_text(encoding="utf-8"))
        by_id = {row["id"]: row for row in patch}
        self.assertEqual(by_id["agent-preset-registry"]["config"]["default"], "shadow-agent")
        self.assertNotIn("agent-presets", by_id)
        self.assertTrue(by_id["session-log-deepseek"]["disabled"])


if __name__ == "__main__":
    unittest.main()
