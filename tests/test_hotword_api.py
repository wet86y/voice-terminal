"""Hotword API（热词接口）：真实校验、持久化及草稿试测隔离。"""
import httpx
import pytest
from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from tests.test_hotwords import settings


@pytest.mark.asyncio
async def test_hotword_save_preview_and_reload(tmp_path):
    config = AppConfig()
    store = ProductSettingsStore(path=tmp_path / "product.json", config=config)
    runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
    app = create_app(runtime, product_settings=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            imported = await client.post("/api/v1/product/hotwords/import", json={"text": "Claude | 克劳得"})
            assert imported.status_code == 200
            assert store.settings_only()["hotwords"]["entries"] == []
            preview = await client.post("/api/v1/product/hotwords/preview", json={"settings": settings(), "text": "请让克劳得检查。"})
            assert preview.status_code == 200 and preview.json()["text"] == "请让Claude检查。"
            assert store.settings_only()["hotwords"]["entries"] == []
            saved = await client.put("/api/v1/product/settings/hotwords", json=settings())
            assert saved.status_code == 200 and saved.json()["effective_from"] == "next_turn"
            assert (await client.get("/api/v1/product/settings/hotwords")).json()["settings"] == settings()
            assert ProductSettingsStore(path=store.path, config=config).settings_only()["hotwords"] == settings()
            invalid = await client.put("/api/v1/product/settings/hotwords", json={**settings(), "threshold": 2})
            assert invalid.status_code == 422
            assert runtime.hotwords.bank.settings == settings()
    finally:
        runtime.hotwords.close()


@pytest.mark.asyncio
async def test_persistence_failure_does_not_activate_new_bank(tmp_path, monkeypatch):
    config = AppConfig()
    store = ProductSettingsStore(path=tmp_path / "product.json", config=config)
    runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
    app = create_app(runtime, product_settings=store)
    try:
        await runtime.hotwords.configure(settings())
        store.save_hotwords(settings())
        monkeypatch.setattr(store, "_save_settings", lambda: (_ for _ in ()).throw(OSError("disk failure")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/hotwords", json={**settings(), "enabled": False})
            assert response.status_code == 500
        assert runtime.hotwords.bank.settings == settings()
        assert store.settings_only()["hotwords"] == settings()
    finally:
        runtime.hotwords.close()
