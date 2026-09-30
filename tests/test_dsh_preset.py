from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class DshPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.preset_dir = self.root / "config" / "dsh" / "agent-presets" / "shadow-agent"

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
        bundle = self.root / "integrations" / "dsh" / "voice-terminal-shadow-bridge"
        manifest = (bundle / "package.json").read_text(encoding="utf-8")
        source = (bundle / "src" / "index.ts").read_text(encoding="utf-8")
        rules = (self.root / "config" / "dsh" / "shadow-agent.rules.md").read_text(encoding="utf-8")
        self.assertIn('"dsh"', manifest)
        for tool in (
            "get_latest_context", "read_ambient_context", "publish_progress",
            "publish_voice_information",
            "request_user_input", "return_shadow_result", "finish_shadow_no_action",
        ):
            self.assertIn(tool, source)
        self.assertIn("agentDefaultModel.currentSelection()", source)
        self.assertIn("agentOptions: modelSelection", source)
        self.assertIn("protocolRecoveryPending", source)
        self.assertIn("status === 'idle'", source)
        self.assertIn("event?.data?.reason", source)
        self.assertIn("content?.[0]?.toolCallId", source)
        self.assertIn("子代理", rules)
        self.assertIn("普通 assistant 正文", rules)

    def test_web_patch_selects_shadow_and_disables_session_upload(self) -> None:
        patch = yaml.safe_load((self.root / "config" / "dsh" / "web-profile.patch.yml").read_text(encoding="utf-8"))
        by_id = {row["id"]: row for row in patch}
        self.assertEqual(by_id["agent-preset-registry"]["config"]["default"], "shadow-agent")
        self.assertNotIn("agent-presets", by_id)
        self.assertTrue(by_id["session-log-deepseek"]["disabled"])


if __name__ == "__main__":
    unittest.main()
