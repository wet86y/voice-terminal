from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.model_registry.service import ModelRegistryService
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app


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
        runtime = SessionRuntime(config, product_settings=settings)
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

    def test_snapshot_is_product_safe_and_settings_routes_are_bounded(self) -> None:
        response = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"schema_version", "runtime", "conversation", "shadow", "settings", "models", "warnings"},
        )
        self.assertIn("current", body["conversation"])
        self.assertIn("services", body["runtime"])
        rendered = repr(body).lower()
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("credential_ref", rendered)

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

        valid_persona = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/persona",
            json={
                "instructions": "instructions",
                "personality": "personality",
                "user": "user",
                "shadow": "shadow",
            },
        ))
        self.assertEqual(valid_persona.status_code, 200)
        self.assertEqual(valid_persona.json()["effective_from"], "next_episode")

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


if __name__ == "__main__":
    unittest.main()
