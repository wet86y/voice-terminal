from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from voice_terminal.config import AppConfig
from voice_terminal.model_registry.service import ModelRegistryError, ModelRegistryService


class FakeVault:
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.values: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return self._available

    @property
    def diagnostic(self) -> str | None:
        return None if self._available else "test vault unavailable"

    def put(self, reference: str, secret: str) -> None:
        self.values[reference] = secret

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model_dir = self.root / "models"
        (self.model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M").mkdir(parents=True)
        (self.model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M" / "tokens.txt").write_text("x", encoding="utf-8")
        self.applied: list[dict] = []

        async def apply(payload: dict) -> dict:
            self.applied.append(payload)
            return {"ok": True, "restarted": ["llm", "shadow_agent", "tts"]}

        self.service = ModelRegistryService(
            config=AppConfig(model_dir=self.model_dir),
            registry_path=self.root / "config" / "model-registry.json",
            vault=FakeVault(),
            apply_callback=apply,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scan_is_scoped_and_hiding_persists_across_scan(self) -> None:
        first = asyncio.run(self.service.scan())
        local = next(item for item in first["models"] if item["capability"] == "local_kws")
        self.assertEqual(local["local_path"], str((self.model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M").resolve()))
        hidden = asyncio.run(self.service.hide(local["id"]))
        self.assertTrue(next(item for item in hidden["models"] if item["id"] == local["id"])["hidden"])
        rescanned = asyncio.run(self.service.scan())
        self.assertTrue(next(item for item in rescanned["models"] if item["id"] == local["id"])["hidden"])
        restored = asyncio.run(self.service.restore(local["id"]))
        self.assertFalse(next(item for item in restored["models"] if item["id"] == local["id"])["hidden"])

    def test_apply_requires_confirmation_and_rejects_hiding_assigned_entry(self) -> None:
        scanned = asyncio.run(self.service.scan())
        voice = next(item for item in scanned["models"] if item["name"] == "deepseek-flash" and item["capability"] == "llm")
        shadow = next(item for item in scanned["models"] if item["name"].startswith("Shadow"))
        status = next(item for item in scanned["models"] if item["name"].startswith("状态汇报"))
        tts = scanned["tts_profiles"][0]
        assignments = {"voice_llm_model_id": voice["id"], "shadow_llm_model_id": shadow["id"], "status_llm_model_id": status["id"], "tts_profile_id": tts["id"], "role_settings": {"voice": {"reasoning_effort": "low", "max_context_tokens": 32768}, "shadow": {"reasoning_effort": "high", "max_context_tokens": 65536}, "status": {"reasoning_effort": "none", "max_context_tokens": 4096}}}
        planned = asyncio.run(self.service.apply_assignments(assignments, confirmed=False))
        self.assertTrue(planned["requires_confirmation"])
        applied = asyncio.run(self.service.apply_assignments(assignments, confirmed=True))
        self.assertTrue(applied["applied"])
        self.assertEqual(len(self.applied), 1)
        self.assertIn("status", self.applied[0])
        self.assertEqual(self.applied[0]["status"]["settings"]["reasoning_effort"], "none")
        self.assertEqual(self.applied[0]["tts"]["profile"]["model_id"], next(item["id"] for item in scanned["models"] if item["capability"] == "tts"))
        with self.assertRaises(ModelRegistryError):
            asyncio.run(self.service.hide(voice["id"]))

    def test_public_snapshot_never_contains_secret_field(self) -> None:
        snapshot = asyncio.run(self.service.scan())
        rendered = repr(snapshot).lower()
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("super-secret", rendered)

        # The browser boundary remains safe even if an older registry placed
        # credential-shaped fields on a model or TTS profile instead of the
        # provider record.
        registry = self.service._load()
        registry["models"][0]["api_key"] = "super-secret"
        registry["tts_profiles"][0]["credential_ref"] = "private-ref"
        self.service._write(registry)
        rendered = repr(asyncio.run(self.service.snapshot())).lower()
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("private-ref", rendered)

    def test_v1_migration_merges_same_product_model_and_derives_shared_policy(self) -> None:
        registry = {
            "schema_version": 1,
            "providers": [
                {"id": "provider-llm", "name": "Managed LLM", "origin": "product_managed", "protocol": "deepseek", "base_url": "https://llm.example", "capabilities": ["llm"], "credential_ref": None, "credential_state": "stored"},
                {"id": "provider-tts", "name": "Managed TTS", "origin": "product_managed", "protocol": "minimax", "base_url": "https://tts.example", "capabilities": ["tts"], "credential_ref": None, "credential_state": "stored"},
            ],
            "models": [
                {"id": "voice-old", "name": "Shared voice", "model_id": "shared-model", "provider_id": "provider-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy"},
                {"id": "shadow-old", "name": "Shared shadow", "model_id": "shared-model", "provider_id": "provider-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy"},
                {"id": "status-old", "name": "Status", "model_id": "status-model", "provider_id": "provider-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy"},
                {"id": "tts-old", "name": "TTS", "model_id": "speech-model", "provider_id": "provider-tts", "capability": "tts", "origin": "product_managed", "verification": "verified", "health": "healthy"},
            ],
            "tts_profiles": [{"id": "tts-profile", "name": "Default", "model_id": "tts-old", "origin": "product_managed", "verification": "verified"}],
            "assignments": {
                "voice_llm_model_id": "voice-old",
                "shadow_llm_model_id": "shadow-old",
                "status_llm_model_id": "status-old",
                "tts_profile_id": "tts-profile",
                "role_settings": {
                    "voice": {"reasoning_effort": "low", "max_context_tokens": 131072},
                    "shadow": {"reasoning_effort": "max", "max_context_tokens": 262144},
                    "status": {"reasoning_effort": "none", "max_context_tokens": 32768},
                },
            },
            "hidden_model_ids": [],
        }
        self.service._write(registry)

        snapshot = asyncio.run(self.service.snapshot())
        shared = [item for item in snapshot["models"] if item.get("model_id") == "shared-model"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["max_context_tokens"], 262144)
        self.assertEqual(set(shared[0]["allowed_reasoning"]), {"off", "low", "high"})
        self.assertEqual(snapshot["assignments"]["voice_llm_model_id"], shared[0]["id"])
        self.assertEqual(snapshot["assignments"]["shadow_llm_model_id"], shared[0]["id"])
        self.assertEqual(snapshot["assignments"]["shadow_reasoning_effort"], "high")
        self.assertEqual(snapshot["assignments"]["role_settings"]["shadow"]["reasoning_effort"], "high")
        self.assertEqual(snapshot["schema_version"], 2)
        persisted = json.loads(self.service.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 2)
        self.assertEqual(persisted["assignments"]["policy_mode"], "legacy")
        self.assertEqual(persisted["assignments"]["shadow_reasoning_effort"], "high")

    def test_model_policy_apply_uses_model_fields_and_failure_keeps_assignment(self) -> None:
        registry = self.service._empty()
        registry["providers"] = [
            {"id": "p-llm", "name": "LLM", "origin": "product_managed", "protocol": "deepseek", "base_url": "https://llm.example", "capabilities": ["llm"], "credential_ref": "ref-llm", "credential_state": "stored"},
            {"id": "p-tts", "name": "TTS", "origin": "product_managed", "protocol": "minimax", "base_url": "https://tts.example", "capabilities": ["tts"], "credential_ref": "ref-tts", "credential_state": "stored"},
        ]
        registry["models"] = [
            {"id": "voice", "name": "Voice", "model_id": "voice-model", "provider_id": "p-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy", "max_context_tokens": 131072, "allowed_reasoning": ["off", "low"]},
            {"id": "shadow", "name": "Shadow", "model_id": "shadow-model", "provider_id": "p-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy", "max_context_tokens": 262144, "allowed_reasoning": ["off", "medium", "high"]},
            {"id": "status", "name": "Status", "model_id": "status-model", "provider_id": "p-llm", "capability": "llm", "origin": "product_managed", "verification": "verified", "health": "healthy", "max_context_tokens": 32768, "allowed_reasoning": ["high"]},
            {"id": "tts", "name": "Speech", "model_id": "speech-model", "provider_id": "p-tts", "capability": "tts", "origin": "product_managed", "verification": "verified", "health": "healthy"},
        ]
        registry["tts_profiles"] = [{"id": "profile", "name": "Speech profile", "model_id": "tts", "origin": "product_managed", "verification": "verified", "output_format": "pcm"}]
        self.service._write(registry)
        self.service.vault.values.update({"ref-llm": "llm-secret", "ref-tts": "tts-secret"})

        normalized = asyncio.run(self.service.snapshot())
        model_ids = {item["model_id"]: item["id"] for item in normalized["models"]}
        voice_id = model_ids["voice-model"]
        shadow_id = model_ids["shadow-model"]
        status_id = model_ids["status-model"]
        tts_id = model_ids["speech-model"]

        assignments = {
            "voice_llm_model_id": voice_id,
            "shadow_llm_model_id": shadow_id,
            "status_llm_model_id": status_id,
            "tts_profile_id": "profile",
            "shadow_reasoning_effort": "medium",
        }
        applied = asyncio.run(self.service.apply_assignments(assignments, confirmed=True))
        self.assertTrue(applied["applied"])
        self.assertEqual(self.applied[0]["voice"]["settings"]["reasoning_effort"], "low")
        self.assertEqual(self.applied[0]["shadow"]["settings"]["reasoning_effort"], "high")
        self.assertEqual(self.applied[0]["status"]["settings"]["reasoning_effort"], "none")
        self.assertEqual(self.applied[0]["tts"]["profile"]["model_id"], tts_id)
        self.assertEqual(self.applied[0]["voice"]["api_key"], "llm-secret")
        self.assertEqual(applied["assignments"]["policy_mode"], "model")
        self.assertEqual(applied["assignments"]["shadow_reasoning_effort"], "high")
        self.assertEqual(applied["assignments"]["role_settings"]["shadow"]["reasoning_effort"], "high")

        off_assignments = {**assignments, "shadow_reasoning_effort": "off"}
        applied_off = asyncio.run(self.service.apply_assignments(off_assignments, confirmed=True))
        self.assertTrue(applied_off["applied"])
        self.assertEqual(self.applied[-1]["shadow"]["settings"]["reasoning_effort"], "high")
        self.assertEqual(applied_off["assignments"]["shadow_reasoning_effort"], "high")
        self.assertEqual(applied_off["assignments"]["role_settings"]["shadow"]["reasoning_effort"], "high")

        public = asyncio.run(self.service.snapshot())
        self.assertNotIn("llm-secret", repr(public))
        self.assertNotIn("tts-secret", repr(public))
        previous = public["assignments"]

        async def fail(_payload: dict) -> dict:
            return {"ok": False, "message": "provider unavailable"}

        self.service.apply_callback = fail
        failed = asyncio.run(self.service.apply_assignments({**assignments, "shadow_reasoning_effort": "high"}, confirmed=True))
        self.assertFalse(failed["applied"])
        self.assertEqual(failed["assignments"], previous)
        self.assertEqual(asyncio.run(self.service.snapshot())["assignments"], previous)

        incompatible = self.service._load()
        shadow_entry = next(
            item for item in incompatible["models"] if item.get("model_id") == "shadow-model"
        )
        shadow_entry["allowed_reasoning"] = ["off", "medium"]
        self.service._write(incompatible)
        with self.assertRaisesRegex(ModelRegistryError, "必须支持 high"):
            asyncio.run(self.service.apply_assignments(assignments, confirmed=True))

        with self.assertRaises(ModelRegistryError):
            asyncio.run(self.service.update_model(voice_id, {"provider_id": "other"}))
        with self.assertRaises(ModelRegistryError):
            asyncio.run(self.service.hide(voice_id))
        with self.assertRaises(ModelRegistryError):
            asyncio.run(self.service.delete_model(voice_id))
