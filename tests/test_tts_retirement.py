"""The retired local synthesizer stays outside the active product runtime."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.tts.router import TtsBackendRouter


def test_legacy_local_settings_migrate_without_starting_a_worker():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = AppConfig.from_mapping({"tts": {"backend": "cosyvoice", "provider": "cosyvoice"}})
        assert config.tts.backend == "minimax"
        assert config.tts.provider == "minimax"
        settings_path = root / "product-settings.json"
        settings_path.write_text(json.dumps({"schema_version": 5, "tts": {"backend": "cosyvoice"}}), encoding="utf-8")
        product = ProductSettingsStore(path=settings_path, config=config)
        runtime = SessionRuntime(config, product_settings=product, runtime_data_dir=root / ".runtime")
        assert product.snapshot()["tts"]["backend"] == "minimax"
        assert json.loads(settings_path.read_text(encoding="utf-8"))["tts"]["backend"] == "minimax"
        assert not hasattr(runtime, "cosyvoice_worker")


def test_status_exposes_only_active_online_backend():
    async def scenario():
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = AppConfig(model_dir=root / "models", persona=PersonaConfig(directory=root / "persona"))
            runtime = SessionRuntime(config, runtime_data_dir=root / ".runtime")
            app = create_app(runtime)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                status = await client.get("/api/v1/tts/status")
                assert status.status_code == 200
                assert status.json() == {"backend": "minimax", "worker": {"state": "stopped"}}
                snapshot = await client.get("/api/v1/product/snapshot")
                assert snapshot.status_code == 200
                assert snapshot.json()["runtime"]["tts_worker"] == {"state": "stopped"}
                switch = await client.put("/api/v1/product/settings/tts", json={"backend": "cosyvoice"})
                assert switch.status_code == 404
    asyncio.run(scenario())


def test_compatibility_router_pins_each_transaction_until_release():
    async def scenario():
        remote = SimpleNamespace(transport="websocket", close=AsyncMock())
        local = SimpleNamespace(transport="worker", ensure_ready=AsyncMock(), close=AsyncMock())
        router = TtsBackendRouter(remote=remote, local=local)
        first_backend, first_client = router.bind()
        await router.switch("cosyvoice")
        second_backend, second_client = router.bind()
        await router.switch("minimax")
        assert (first_backend, first_client) == ("minimax", remote)
        assert (second_backend, second_client) == ("cosyvoice", local)
        local.close.assert_not_awaited()
        router.release(second_backend)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        local.close.assert_awaited_once()
        await router.shutdown()
        remote.close.assert_awaited_once()
    asyncio.run(scenario())
