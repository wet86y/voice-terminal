from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx
from unittest.mock import AsyncMock, PropertyMock, patch

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.config import COMPUTE_MODEL_IDS
from voice_terminal.model_registry.service import ModelRegistryService
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.session.conversation_log import SYSTEM_SPEECH, USER_TURN


class FakeVault:
    @property
    def available(self) -> bool:
        return True

    @property
    def diagnostic(self) -> str | None:
        return None

    def put(self, _reference: str, _secret: str) -> None:
        pass

    def get(self, _reference: str) -> str | None:
        return None

    def delete(self, _reference: str) -> None:
        pass


class ProductApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = AppConfig(
            model_dir=self.root / "models",
            persona=PersonaConfig(directory=self.root / "config" / "persona"),
        )
        settings = ProductSettingsStore(
            path=self.root / "config" / "product-settings.json",
            config=config,
        )
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=self.root / ".runtime")
        self.runtime = runtime
        registry = ModelRegistryService(
            config=config,
            registry_path=self.root / "config" / "model-registry.json",
            vault=FakeVault(),
        )
        self.app = create_app(
            runtime,
            model_registry=registry,
            product_settings=settings,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    def test_compute_api_saves_device_choices_for_full_application_restart(self) -> None:
        devices = {
            name: ("auto" if name == "paraformer" else "cpu")
            for name in COMPUTE_MODEL_IDS
        }
        status_models = {
            name: {"device": "cpu", "requested_device": devices[name]}
            for name in COMPUTE_MODEL_IDS
        }
        with patch.object(
            self.runtime,
            "compute_status",
            new=AsyncMock(return_value={"models": status_models, "hardware": {}}),
        ):
            saved = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"devices": devices},
            ))
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["restart_required"])
        self.assertEqual(saved.json()["effective_from"], "application_restart")
        self.assertEqual(self.runtime.product_settings.snapshot()["compute"]["devices"], devices)

        with patch.object(
            self.runtime,
            "compute_status",
            new=AsyncMock(return_value={"models": status_models, "hardware": {}}),
        ):
            status = asyncio.run(self._request("GET", "/api/v1/product/compute-status"))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(set(status.json()), {"hardware", "models"})
        self.assertEqual(set(status.json()["models"]), set(COMPUTE_MODEL_IDS))

    def test_compute_api_rejects_gpu_when_model_backend_is_missing(self) -> None:
        unavailable = {
            "models": {
                name: {"gpu_available": False, "reason": "后端未安装"}
                for name in COMPUTE_MODEL_IDS
            },
        }
        devices = {
            name: ("cuda" if name == "sensevoice" else "cpu")
            for name in COMPUTE_MODEL_IDS
        }
        with patch("voice_terminal.server.websocket.hardware_status", return_value=unavailable):
            response = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"devices": devices},
            ))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.runtime.product_settings.snapshot()["compute"]["devices"]["sensevoice"],
            "cpu",
        )

    def test_owner_only_barge_in_requires_profile_and_worker_even_when_mode_off(self) -> None:
        payload = {
            "mode": "off", "confirm_threshold": 0.5,
            "continuity_threshold": 0.4, "owner_only_barge_in": True,
        }
        self.assertFalse(self.runtime.product_settings.snapshot()["speaker"]["owner_only_barge_in"])
        response = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/speaker", json=payload,
        ))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.runtime.product_settings.snapshot()["speaker"]["owner_only_barge_in"])

    def test_owner_only_barge_in_saves_independently_of_speaker_mode(self) -> None:
        payload = {
            "mode": "off", "confirm_threshold": 0.5,
            "continuity_threshold": 0.4, "owner_only_barge_in": True,
        }
        with patch("voice_terminal.speaker.registry.SpeakerProfileStore.available", new_callable=PropertyMock, return_value=True), \
             patch.object(self.runtime, "ensure_speaker_worker", new=AsyncMock(return_value=True)), \
             patch.object(self.runtime, "set_speaker_settings", new=AsyncMock(return_value={
                 "owner_only_barge_in_selected": True,
                 "owner_only_barge_in_effective": True,
                 "owner_only_barge_in_reason": None,
             })) as apply_settings:
            response = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/speaker", json=payload,
            ))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["settings"]["speaker"]["owner_only_barge_in"])
        self.assertEqual(apply_settings.await_args.args[0]["mode"], "off")
        self.assertTrue(apply_settings.await_args.args[0]["owner_only_barge_in"])

    def test_snapshot_is_product_safe_and_settings_routes_are_bounded(self) -> None:
        response = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"schema_version", "runtime", "conversation", "shadow", "dsh", "settings", "models", "warnings"},
        )
        self.assertIn("current", body["conversation"])
        self.assertIn("services", body["runtime"])
        rendered = repr(body).lower()
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("credential_ref", rendered)
        self.assertNotIn("token=", rendered)

        invalid_silence = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/silence", json={"seconds": 4},
        ))
        self.assertEqual(invalid_silence.status_code, 422)

        invalid_persona = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/persona",
            json={"instructions": "", "personality": "", "user": "", "shadow": "", "path": "x"},
        ))
        self.assertEqual(invalid_persona.status_code, 422)

        valid_wake = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/wake",
            json={
                "enabled": True,
                "keyword": "hei claw",
                "sensitivity": 75,
            },
        ))
        self.assertEqual(valid_wake.status_code, 200)
        self.assertEqual(valid_wake.json()["effective_from"], "next_session")
        self.assertTrue(valid_wake.json()["pending"])

        invalid_shadow = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/shadow",
            json={"experimental_context_gate_enabled": True, "extra": False},
        ))
        self.assertEqual(invalid_shadow.status_code, 422)
        valid_shadow = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/shadow",
            json={"experimental_context_gate_enabled": True},
        ))
        self.assertEqual(valid_shadow.status_code, 200)
        self.assertEqual(valid_shadow.json()["effective_from"], "immediate")
        self.assertFalse(valid_shadow.json()["pending"])
        self.assertTrue(valid_shadow.json()["settings"]["shadow"]["experimental_context_gate_enabled"])

        valid_persona = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/persona",
            json={
                "personality": "personality",
                "user": "user",
            },
        ))
        self.assertEqual(valid_persona.status_code, 200)
        self.assertEqual(valid_persona.json()["effective_from"], "next_episode")
        entry = {
            "id": "search", "trigger": "帮我查查", "target": "shadow",
            "instruction": "查询用户所问的资料。", "enabled": True,
        }
        saved = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/custom-instructions",
            json={"custom_instructions": [entry]},
        ))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["effective_from"], "next_session")
        self.assertEqual(saved.json()["settings"]["custom_instructions"], [entry])
        invalid = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/custom-instructions",
            json={"custom_instructions": [entry, entry]},
        ))
        self.assertEqual(invalid.status_code, 422)

    def test_bootstrap_returns_the_server_ordered_conversation(self) -> None:
        ledger = self.runtime.history
        ledger.conversation.append(
            USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="灵月是什么游戏？",
            status="completed", delivery={"status": "completed", "termination": "completed"},
        )
        ledger.conversation.append(
            SYSTEM_SPEECH, conversation_id="conversation_a", turn_id="wake_1", speech_id="speech_wake",
            text="我在，请说。", status="completed",
            delivery={"status": "full", "termination": "completed"},
        )

        response = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))

        self.assertEqual(response.status_code, 200)
        conversation = response.json()["conversation"]
        self.assertEqual(conversation["latest_seq"], 2)
        self.assertEqual(
            [(entry["kind"], entry["seq"], entry["text"]) for entry in conversation["entries"]],
            [(USER_TURN, 1, "灵月是什么游戏？"), (SYSTEM_SPEECH, 2, "我在，请说。")],
        )

    def test_cors_allows_local_product_origins_but_not_wildcard(self) -> None:
        allowed = asyncio.run(self._request(
            "OPTIONS", "/api/v1/product/snapshot",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        ))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://localhost:5173")

        blocked = asyncio.run(self._request(
            "OPTIONS", "/api/v1/product/snapshot",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        ))
        self.assertNotEqual(blocked.headers.get("access-control-allow-origin"), "*")
        self.assertIsNone(blocked.headers.get("access-control-allow-origin"))

    def test_dsh_launch_is_origin_json_and_ready_gated(self) -> None:
        status = asyncio.run(self._request("GET", "/api/v1/dsh/status"))
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("token", repr(status.json()).lower())

        blocked = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://evil.example"},
        ))
        self.assertEqual(blocked.status_code, 403)

        wrong_type = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", content="{}",
            headers={"Origin": "http://127.0.0.1:5173", "Content-Type": "text/plain"},
        ))
        self.assertEqual(wrong_type.status_code, 415)

        self.runtime.dsh._state = "starting"
        starting = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://127.0.0.1:5173"},
        ))
        self.assertEqual(starting.status_code, 409)

        self.runtime.dsh._state = "ready"
        self.runtime.dsh._launch_url = "http://127.0.0.1:3081/?token=secret-launch-token"
        ready = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://127.0.0.1:5173"},
        ))
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.headers["cache-control"], "no-store")
        self.assertEqual(ready.json()["url"], self.runtime.dsh._launch_url)

        snapshot = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))
        self.assertNotIn("secret-launch-token", snapshot.text)


if __name__ == "__main__":
    unittest.main()
