from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_terminal.agent.episode import AgentDecisionScheduler
from voice_terminal.agent.options import AgentOptions
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.product import (
    MAX_PERSONA_DOCUMENT_BYTES,
    ProductSettingsError,
    ProductSettingsStore,
    ShadowProductProjection,
)
from voice_terminal.shadow_agent.contracts import ShadowEvent


class ProductSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model_dir = self.root / "models"
        self.config = AppConfig(
            model_dir=self.model_dir,
            persona=PersonaConfig(directory=self.root / "config" / "persona"),
        )
        self.settings_path = self.root / "config" / "product-settings.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self) -> ProductSettingsStore:
        return ProductSettingsStore(path=self.settings_path, config=self.config)

    def test_settings_are_atomic_reloadable_and_corruption_is_reported(self) -> None:
        store = self.store()
        asyncio.run(store.save_silence({"seconds": 60}))
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8"))["silence"]["seconds"], 60)
        self.assertFalse(any(self.settings_path.parent.glob(".product-settings-*.json")))

        reloaded = self.store()
        self.assertEqual(reloaded.snapshot()["silence"]["seconds"], 60)

        self.settings_path.write_text("{not-json", encoding="utf-8")
        corrupted = self.store()
        self.assertTrue(corrupted.warnings)
        self.assertEqual(corrupted.snapshot()["silence"]["seconds"], 30)
        # Loading a damaged file is read-only: recovery never overwrites the
        # user's artifact without an explicit save.
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{not-json")

    def test_kws_compile_replaces_files_only_after_validation(self) -> None:
        kws_dir = self.config.kws_dir
        kws_dir.mkdir(parents=True)
        keywords_file = self.config.kws_keywords_file
        raw_file = keywords_file.parent / "keywords_raw.txt"
        keywords_file.write_text("old @HEI_CLAW\n", encoding="utf-8")
        raw_file.write_text("HEI CLAW @HEI_CLAW\n", encoding="utf-8")
        store = self.store()
        old_settings = store.settings_only()["wake"]
        old_keywords = keywords_file.read_text(encoding="utf-8")
        old_raw = raw_file.read_text(encoding="utf-8")

        def fake_generate(*, raw_path, output_path, label, **_kwargs):
            raw_path.write_text(f"NEW CLAW @{label}\n", encoding="utf-8")
            output_path.write_text(f"phones @{label}\n", encoding="utf-8")

        with patch("voice_terminal.kws.keywords.generate_keywords", fake_generate), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            result = asyncio.run(store.save_wake({
                "enabled": True,
                "always_on": True,
                "keyword": "new claw",
                "sensitivity": 25,
            }))
        self.assertEqual(result["wake"]["keyword"], "new claw")
        self.assertIn("@NEW_CLAW", keywords_file.read_text(encoding="utf-8"))
        self.assertIn("@NEW_CLAW", raw_file.read_text(encoding="utf-8"))
        self.assertNotEqual(old_keywords, keywords_file.read_text(encoding="utf-8"))
        self.assertNotEqual(old_raw, raw_file.read_text(encoding="utf-8"))

        # A text2token/dictionary failure occurs before settings mutation and
        # before either live keyword file is replaced.
        failing = self.store()
        previous = failing.settings_only()["wake"]
        previous_keywords = keywords_file.read_text(encoding="utf-8")
        previous_raw = raw_file.read_text(encoding="utf-8")
        with patch("voice_terminal.kws.keywords.generate_keywords", side_effect=RuntimeError("dictionary unsupported")), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            with self.assertRaises(ProductSettingsError):
                asyncio.run(failing.save_wake({
                    "enabled": False,
                    "always_on": False,
                    "keyword": "another claw",
                    "sensitivity": 40,
                }))
        self.assertEqual(failing.settings_only()["wake"], previous)
        self.assertEqual(keywords_file.read_text(encoding="utf-8"), previous_keywords)
        self.assertEqual(raw_file.read_text(encoding="utf-8"), previous_raw)
        self.assertEqual(old_settings["keyword"], "hei claw")

    def test_kws_and_silence_ranges_and_persona_allowlist(self) -> None:
        store = self.store()
        with self.assertRaises(ProductSettingsError):
            asyncio.run(store.save_silence({"seconds": 4}))
        with self.assertRaises(ProductSettingsError):
            asyncio.run(store.save_wake({
                "enabled": True, "always_on": False,
                "keyword": "hei claw", "sensitivity": 100,
            }))

        payload = {
            "instructions": "主指令",
            "personality": "温和",
            "user": "用户资料",
            "shadow": "Shadow 指令",
        }
        snapshot = store.save_persona(payload)
        self.assertEqual(snapshot["persona"], payload)
        self.assertEqual(
            (self.config.persona.directory / "instructions.md").read_text(encoding="utf-8"),
            "主指令",
        )
        self.assertEqual(
            (self.config.persona.directory.parent / "shadow_agent" / "instructions.md").read_text(encoding="utf-8"),
            "Shadow 指令",
        )
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "path": "../../outside.txt"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "user": "bad\x00value"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "user": "x" * (MAX_PERSONA_DOCUMENT_BYTES + 1)})

    def test_shadow_projection_is_bounded_safe_and_does_not_fabricate_telemetry(self) -> None:
        projection = ShadowProductProjection()
        projection.reset(available=True)
        for index in range(6):
            projection.update_events([ShadowEvent(
                event_id=f"secret-event-{index}",
                kind="progress" if index < 5 else "result",
                text=f"  进度 {index}   \n 内部 trace {index} ",
                based_on_context_revision=9000 + index,
                wake_policy="passive" if index < 5 else "priority",
                payload={"event_id": "must-not-leak", "prompt": "private"},
            )])
        safe = projection.snapshot({
            "available": True,
            "active": False,
            "context_token_estimate": 0,
            "context_limit_tokens": 256000,
            "context_usage_percent": 0,
            "provider_input_tokens": None,
            "provider_cached_tokens": None,
            "provider_cache_hit_ratio": None,
        })
        self.assertEqual(len(safe["timeline"]), 5)
        self.assertEqual(safe["context"], {})
        self.assertEqual(safe["provider"], {})
        rendered = repr(safe)
        self.assertNotIn("secret-event", rendered)
        self.assertNotIn("based_on_context_revision", rendered)
        self.assertNotIn("prompt", rendered)
        self.assertLessEqual(max(len(item["text"]) for item in safe["timeline"]), 320)

        with_telemetry = projection.snapshot({
            "available": True,
            "active": True,
            "context_token_estimate": 86000,
            "context_limit_tokens": 256000,
            "context_usage_percent": 34,
            "provider_input_tokens": 86000,
            "provider_cached_tokens": 81700,
            "provider_cache_hit_ratio": 0.95,
        })
        self.assertEqual(with_telemetry["context"]["used_tokens"], 86000)
        self.assertEqual(with_telemetry["provider"]["cache_hit_ratio"], 0.95)
        projection.reset(available=False)
        self.assertEqual(projection.snapshot()["phase"], "unavailable")
        self.assertFalse(projection.snapshot()["active"])

    def test_final_silence_scheduler_window_is_dynamic(self) -> None:
        expected = {5: 13, 30: 75, 60: 150, 90: 225}
        for seconds, final_window in expected.items():
            scheduler = AgentDecisionScheduler(AgentOptions(final_silence_ms=seconds * 1000))
            self.assertEqual(scheduler.FINAL_WINDOW, final_window)
            self.assertTrue(all(window < final_window for window in scheduler.FORCE_WINDOWS))
        self.assertEqual(AgentDecisionScheduler(AgentOptions()).FINAL_WINDOW, 75)


if __name__ == "__main__":
    unittest.main()
