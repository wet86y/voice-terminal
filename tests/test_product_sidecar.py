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

    def test_speaker_relaxation_persists_even_below_legacy_continuity_threshold(self) -> None:
        store = self.store()
        store.save_speaker({"mode": "off", "owner_only_barge_in": False,
                            "confirm_threshold": 0.30, "continuity_relaxation": 0.20})
        saved = self.store().snapshot()["speaker"]
        self.assertEqual(saved["confirm_threshold"], 0.30)
        self.assertEqual(saved["continuity_relaxation"], 0.20)
        self.assertAlmostEqual(saved["continuity_threshold"], 0.10)
        store.save_speaker({"mode": "off", "owner_only_barge_in": False,
                            "confirm_threshold": 0.10, "continuity_relaxation": 0.0})
        self.assertEqual(self.store().snapshot()["speaker"]["continuity_threshold"], 0.10)

    def test_speaker_relaxation_rejects_out_of_range_values(self) -> None:
        for value in (-0.01, 0.21, float("nan")):
            with self.assertRaises(ProductSettingsError):
                self.store().save_speaker({"mode": "off", "owner_only_barge_in": False,
                                          "confirm_threshold": 0.60, "continuity_relaxation": value})

    def test_pending_approval_survives_output_history_and_preserves_command_lines(self) -> None:
        projection = ShadowProductProjection()
        projection.update_activities([{"run_seq": 1, "item_id": "approval", "stream": "approval", "phase": "started",
                                       "approval_id": "request", "text": "第一行\n第二行"}])
        for index in range(120):
            projection.update_activities([{"run_seq": 1, "item_id": f"output-{index}", "stream": "output",
                                           "phase": "delta", "delta": "执行输出\n"}])
        approvals = [item for item in projection.activities if item["stream"] == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["text"], "第一行\n第二行")
        self.assertTrue(projection.user_input_required)
        self.assertEqual(projection.phase, "waiting")

    def test_runtime_snapshot_clears_stale_approvals_and_tracks_live_jobs(self) -> None:
        projection = ShadowProductProjection()
        projection.update_activities([
            {"run_seq": 1, "item_id": "approval-old", "stream": "approval", "phase": "started", "approval_id": "old"},
            {"run_seq": 1, "item_id": "approval-live", "stream": "approval", "phase": "started", "approval_id": "live"},
            {"run_seq": 1, "item_id": "job-timer", "stream": "output", "phase": "started"},
        ])
        runtime = {"available": True, "active": False, "user_input_required": True, "pending_approval_ids": ["live"]}
        snapshot = projection.snapshot(runtime)
        self.assertEqual(snapshot["activities"][0]["phase"], "failed")
        self.assertEqual(snapshot["activities"][1]["phase"], "started")
        self.assertEqual(snapshot["phase"], "waiting")
        runtime.update(user_input_required=False, pending_approval_ids=[])
        self.assertEqual(projection.snapshot(runtime)["phase"], "working")
        projection.update_activities([{"run_seq": 1, "item_id": "job-timer", "stream": "output", "phase": "completed"}])
        snapshot = projection.snapshot(runtime)
        self.assertFalse(snapshot["active"])
        self.assertFalse(snapshot["user_input_required"])
        self.assertNotEqual(snapshot["phase"], "working")

    def test_cancelled_run_does_not_leave_background_job_permanently_active(self) -> None:
        projection = ShadowProductProjection()
        projection.update_activities([{"run_seq": 1, "item_id": "job-running", "stream": "output", "phase": "started"}])
        runtime = {"available": True, "active": False, "user_input_required": False}
        self.assertTrue(projection.snapshot(runtime)["active"])
        projection.update_activities([{"run_seq": 1, "item_id": "run-1", "stream": "lifecycle",
                                      "phase": "completed", "state": "cancelled"}])
        self.assertFalse(projection.snapshot(runtime)["active"])
        self.assertEqual(projection.activities[0]["phase"], "failed")

    def test_approval_preferences_persist_without_a_continuous_grant(self) -> None:
        store = self.store()
        self.assertEqual(store.settings_only()["approvals"], {"voice_confirmation_enabled": False, "continuous_enabled": False})
        preferences = {"voice_confirmation_enabled": True, "continuous_enabled": True}
        store.save_approvals(preferences)
        self.assertEqual(self.store().settings_only()["approvals"], preferences)
        with self.assertRaises(ProductSettingsError):
            store.save_approvals({**preferences, "continuous_enabled": "true"})
        with self.assertRaises(ProductSettingsError):
            store.save_approvals({**preferences, "lease": True})

    def test_compute_settings_migrate_default_and_restore_after_gpu_failure(self) -> None:
        legacy = ProductSettingsStore(
            path=self.settings_path,
            config=self.config,
        )
        devices = legacy.snapshot()["compute"]["devices"]
        self.assertEqual(devices["paraformer"], "auto")
        self.assertEqual(
            {key: value for key, value in devices.items() if key != "paraformer"},
            {key: "cpu" for key in devices if key != "paraformer"},
        )

        selected = dict(devices)
        selected["sensevoice"] = "cuda"
        legacy.save_compute(
            {"devices": selected},
            active_devices={key: "cpu" for key in selected},
        )
        self.assertEqual(self.store().snapshot()["compute"]["devices"]["sensevoice"], "cuda")

        reverted = self.store().revert_failed_compute_devices({
            "sensevoice": {
                "device": "cpu",
                "requested_device": "cuda",
                "load_error": "CUDA 初始化失败",
            },
        })
        self.assertEqual(reverted, {"sensevoice": "cpu"})
        saved = self.store().snapshot()["compute"]
        self.assertEqual(saved["devices"]["sensevoice"], "cpu")
        self.assertEqual(saved["fallback_devices"]["sensevoice"], "cpu")

    def test_compute_settings_reject_incomplete_or_invalid_device_maps(self) -> None:
        store = self.store()
        with self.assertRaises(ProductSettingsError):
            store.save_compute({"devices": {"sensevoice": "cuda"}})
        devices = dict(store.snapshot()["compute"]["devices"])
        devices["sensevoice"] = "gpu"
        with self.assertRaises(ProductSettingsError):
            store.save_compute({"devices": devices})

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

        self.settings_path.write_text(json.dumps({"schema_version": []}), encoding="utf-8")
        malformed = self.store()
        self.assertTrue(malformed.warnings)
        self.assertEqual(malformed.snapshot()["schema_version"], 2)

    def test_shadow_gate_defaults_off_and_persists_without_losing_old_settings(self) -> None:
        store = self.store()
        self.assertFalse(store.snapshot()["shadow"]["experimental_context_gate_enabled"])
        saved = store.save_shadow({"experimental_context_gate_enabled": True})
        self.assertTrue(saved["experimental_context_gate_enabled"])
        reloaded = self.store()
        self.assertTrue(reloaded.snapshot()["shadow"]["experimental_context_gate_enabled"])
        self.assertTrue(reloaded.effective_config().shadow_agent.experimental_context_gate_enabled)

        self.settings_path.write_text(json.dumps({
            "schema_version": 1,
            "silence": {"seconds": 45},
        }), encoding="utf-8")
        upgraded = self.store()
        self.assertEqual(upgraded.snapshot()["silence"]["seconds"], 45)
        self.assertFalse(upgraded.snapshot()["shadow"]["experimental_context_gate_enabled"])
        self.assertFalse(upgraded.warnings)

    def test_legacy_speaker_preferences_survive_native_model_migration(self) -> None:
        """保留阈值和筛选偏好；有效观察模式由运行时档案状态决定。"""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({
            "schema_version": 3,
            "speaker": {"mode": "filter", "confirm_threshold": 0.81, "continuity_threshold": 0.72},
        }), encoding="utf-8")
        migrated = self.store()
        speaker = migrated.snapshot()["speaker"]
        self.assertEqual(speaker["confirm_threshold"], 0.81)
        self.assertEqual(speaker["continuity_threshold"], 0.72)
        self.assertEqual(speaker["mode"], "filter")
        self.assertTrue(any("声纹特征管线" in item for item in migrated.warnings))
        # A v1 file with no speaker section has nothing to migrate.
        self.settings_path.write_text(json.dumps({
            "schema_version": 1, "silence": {"seconds": 45},
        }), encoding="utf-8")
        untouched = self.store()
        self.assertFalse(untouched.warnings)
        self.assertEqual(untouched.snapshot()["silence"]["seconds"], 45)

    def test_current_schema_keeps_user_speaker_thresholds(self) -> None:
        store = self.store()
        saved = store.save_speaker({
            "mode": "observe", "confirm_threshold": 0.72, "continuity_threshold": 0.61,
            "owner_only_barge_in": True,
        })
        self.assertEqual(saved["confirm_threshold"], 0.72)
        self.assertTrue(saved["owner_only_barge_in"])
        reloaded = self.store()
        self.assertEqual(reloaded.snapshot()["speaker"]["confirm_threshold"], 0.72)
        self.assertTrue(reloaded.snapshot()["speaker"]["owner_only_barge_in"])
        self.assertFalse(reloaded.warnings)

    def test_owner_only_barge_in_defaults_off_for_old_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({
            "schema_version": 7,
            "speaker": {"mode": "off", "confirm_threshold": 0.6, "continuity_threshold": 0.5},
        }), encoding="utf-8")
        self.assertFalse(self.store().snapshot()["speaker"]["owner_only_barge_in"])

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

        def fake_generate(*, raw_path, output_path, entries, **_kwargs):
            raw_path.write_text(
                "".join(f"{keyword} @{label}\n" for keyword, label in entries), encoding="utf-8",
            )
            output_path.write_text(
                "".join(f"phones @{label}\n" for _keyword, label in entries), encoding="utf-8",
            )

        with patch("voice_terminal.kws.keywords.generate_keywords_for", fake_generate), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            result = asyncio.run(store.save_wake({
                "enabled": True,
                "keyword": "new claw",
                "sensitivity": 75,
            }))
        self.assertEqual(result["wake"]["keyword"], "new claw")
        self.assertEqual(result["wake"]["keywords"], ["new claw"])
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
        with patch("voice_terminal.kws.keywords.generate_keywords_for", side_effect=RuntimeError("dictionary unsupported")), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            with self.assertRaises(ProductSettingsError):
                asyncio.run(failing.save_wake({
                    "enabled": False,
                    "keyword": "another claw",
                    "sensitivity": 40,
                }))
        self.assertEqual(failing.settings_only()["wake"], previous)
        self.assertEqual(keywords_file.read_text(encoding="utf-8"), previous_keywords)
        self.assertEqual(raw_file.read_text(encoding="utf-8"), previous_raw)
        self.assertEqual(old_settings["keyword"], "hei claw")

    def test_multiple_wake_phrases_compile_into_one_label_set(self) -> None:
        """Up to five spellings of one wake word share the managed file."""

        kws_dir = self.config.kws_dir
        kws_dir.mkdir(parents=True)
        keywords_file = self.config.kws_keywords_file
        raw_file = keywords_file.parent / "keywords_raw.txt"
        store = self.store()

        def fake_generate(*, raw_path, output_path, entries, **_kwargs):
            raw_path.write_text(
                "".join(f"{keyword} @{label}\n" for keyword, label in entries), encoding="utf-8",
            )
            output_path.write_text(
                "".join(f"phones @{label}\n" for _keyword, label in entries), encoding="utf-8",
            )

        with patch("voice_terminal.kws.keywords.generate_keywords_for", fake_generate), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            result = asyncio.run(store.save_wake({
                "enabled": True,
                "keywords": ["七三", "你好七三", "七三同学"],
                "sensitivity": 99,
            }))

        self.assertEqual(result["wake"]["keywords"], ["七三", "你好七三", "七三同学"])
        self.assertEqual(result["wake"]["keyword"], "七三")
        self.assertEqual(result["wake"]["keyword_label"], "七三")
        compiled = keywords_file.read_text(encoding="utf-8")
        for label in ("@七三", "@你好七三", "@七三同学"):
            self.assertIn(label, compiled)
        self.assertEqual(len(compiled.strip().splitlines()), 3)
        self.assertEqual(len(raw_file.read_text(encoding="utf-8").strip().splitlines()), 3)

        effective = store.effective_config().wake
        self.assertEqual(effective.keywords, ("七三", "你好七三", "七三同学"))
        self.assertEqual(effective.keyword, "七三")
        self.assertEqual(effective.keyword_labels, ("七三", "你好七三", "七三同学"))

        # A reload must reproduce the same list from the persisted file.
        self.assertEqual(self.store().snapshot()["wake"]["keywords"], ["七三", "你好七三", "七三同学"])

    def test_wake_phrase_list_is_deduped_and_capped(self) -> None:
        kws_dir = self.config.kws_dir
        kws_dir.mkdir(parents=True)
        store = self.store()

        def fake_generate(*, raw_path, output_path, entries, **_kwargs):
            raw_path.write_text("\n".join(f"{keyword} @{label}" for keyword, label in entries), encoding="utf-8")
            output_path.write_text("\n".join(f"phones @{label}" for _keyword, label in entries), encoding="utf-8")

        with patch("voice_terminal.kws.keywords.generate_keywords_for", fake_generate), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            # Repeated spellings and case/alias variants collapse into one entry;
            # "七三" and "七 三" are genuinely different phrases and both stay.
            deduped = asyncio.run(store.save_wake({
                "enabled": True,
                "keywords": ["七三", "七三", "你好七三", "HEI_CLAW"],
                "sensitivity": 75,
            }))
            self.assertEqual(deduped["wake"]["keywords"], ["七三", "你好七三", "hei claw"])

            with self.assertRaises(ProductSettingsError):
                asyncio.run(store.save_wake({
                    "enabled": True,
                    "keywords": ["一", "二", "三", "四", "五", "六"],
                    "sensitivity": 75,
                }))
            with self.assertRaises(ProductSettingsError):
                asyncio.run(store.save_wake({"enabled": True, "keywords": [], "sensitivity": 75}))
            with self.assertRaises(ProductSettingsError):
                asyncio.run(store.save_wake({"enabled": True, "keywords": ["七三", 7], "sensitivity": 75}))
        # Rejected saves never touched the persisted list.
        self.assertEqual(store.snapshot()["wake"]["keywords"], ["七三", "你好七三", "hei claw"])

    def test_kws_and_silence_ranges_and_persona_allowlist(self) -> None:
        store = self.store()
        with self.assertRaises(ProductSettingsError):
            asyncio.run(store.save_silence({"seconds": 4}))
        with self.assertRaises(ProductSettingsError):
            asyncio.run(store.save_wake({
                "enabled": True,
                "keyword": "hei claw", "sensitivity": 100,
            }))

        payload = {
            "personality": "温和",
            "user": "用户资料",
        }
        snapshot = store.save_persona(payload)
        self.assertEqual(snapshot["persona"], payload)
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "instructions": "不能修改内部指令"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "shadow": "不能修改影子规则"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "path": "../../outside.txt"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "user": "bad\x00value"})
        with self.assertRaises(ProductSettingsError):
            store.save_persona({**payload, "user": "x" * (MAX_PERSONA_DOCUMENT_BYTES + 1)})

    def test_chinese_wake_keyword_is_compiled_and_persisted(self) -> None:
        kws_dir = self.config.kws_dir
        kws_dir.mkdir(parents=True)
        store = self.store()

        def fake_generate(*, raw_path, output_path, entries, **_kwargs):
            raw_path.write_text(
                "".join(f"嘿 小爪 @{label}\n" for _keyword, label in entries), encoding="utf-8",
            )
            output_path.write_text(
                "".join(f"h ei @{label}\n" for _keyword, label in entries), encoding="utf-8",
            )

        with patch("voice_terminal.kws.keywords.generate_keywords_for", fake_generate), \
             patch("voice_terminal.kws.detector.discover_model_paths", return_value=object()):
            result = asyncio.run(store.save_wake({
                "enabled": True,
                "keyword": "嘿，小爪",
                "sensitivity": 70,
            }))

        self.assertEqual(result["wake"]["keyword"], "嘿 小爪")
        self.assertEqual(result["wake"]["keyword_label"], "嘿_小爪")
        self.assertEqual(result["wake"]["keywords"], ["嘿 小爪"])
        self.assertNotIn("always_on", result["wake"])
        self.assertAlmostEqual(store.effective_config().wake.threshold, 0.30)
        self.assertIn("@嘿_小爪", (kws_dir / "keywords.txt").read_text(encoding="utf-8"))

    def test_shadow_projection_is_bounded_safe_and_does_not_fabricate_telemetry(self) -> None:
        projection = ShadowProductProjection()
        projection.reset(available=True)
        for index in range(6):
            projection.update_events([ShadowEvent(
                event_id=f"secret-event-{index}",
                kind="progress" if index < 5 else "result",
                text=f"  进度 {index}   \n 内部 trace {index} ",
                based_on_context_revision=9000 + index,
            )])
        safe = projection.snapshot({
            "available": True,
            "active": False,
            "context_token_estimate": 1,
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
            self.assertEqual(scheduler.secondary_interval_windows, 10)
            scheduler.arm(0.0, turn_id="turn-a")
            scheduler.advance(4.0)
            self.assertTrue(scheduler.secondary_due(0))
            self.assertIsNone(scheduler.forced_due(now=4.0))
            scheduler.advance(final_window * scheduler.interval)
            self.assertEqual(scheduler.forced_due(now=final_window * scheduler.interval)["prompt_kind"], "final_silence")
            self.assertIsNone(scheduler.secondary_candidate(now=final_window * scheduler.interval))
        self.assertEqual(AgentDecisionScheduler(AgentOptions()).FINAL_WINDOW, 75)


if __name__ == "__main__":
    unittest.main()
