from __future__ import annotations

import asyncio
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
