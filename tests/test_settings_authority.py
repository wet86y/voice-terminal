"""服务端设置权威：真实路由、提交顺序、保存失败及异步运行配置回归。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app


def instruction(key, name=None):
    return dict(id=key, name=name or key, trigger="要求静默", instruction="执行静默",
                target="shadow", enabled=True, keywords=["静默"])


@pytest.fixture
def runtime(tmp_path):
    config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"))
    store = ProductSettingsStore(path=tmp_path / "config/product-settings.json", config=config)
    return SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "runtime")


@pytest.mark.parametrize("section", ["shadow", "custom_instructions"])
def test_failed_write_restores_memory_and_cannot_leak_into_later_save(tmp_path, section):
    store = ProductSettingsStore(path=tmp_path / "settings.json", config=AppConfig())
    store.save_custom_instructions([instruction("old")])
    before, stamp = store.settings_only(), store.sync_state()
    with patch("voice_terminal.server.product._atomic_write_json", side_effect=OSError("模拟写盘失败")):
        with pytest.raises(OSError, match="模拟写盘失败"):
            if section == "shadow":
                store.save_shadow({"experimental_context_gate_enabled": True})
            else:
                store.save_custom_instructions([instruction("new")])
    assert store.settings_only() == before
    assert store.sync_state() == stamp
    store.save_approvals({"voice_confirmation_enabled": False, "continuous_enabled": False})
    disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert disk[section] == before[section]
    assert "sync" not in disk
    assert store.sync_state()["revision"] == stamp["revision"] + 1
    assert ProductSettingsStore(path=store.path, config=AppConfig()).sync_state()["instance_id"] != stamp["instance_id"]


def test_single_instruction_requests_use_current_server_list(runtime):
    async def run():
        store = runtime.product_settings
        store.save_custom_instructions([instruction("first"), instruction("second")])
        app = create_app(runtime, product_settings=store)
        with patch.object(runtime.instruction_matcher, "configure"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for key, name in (("first", "第一端名称"), ("second", "第二端名称")):
                    result = await client.put("/api/v1/product/settings/custom-instructions", json={
                        "operation": "save", "id": key, "value": instruction(key, name)})
                    assert result.status_code == 200
                toggled = await client.put("/api/v1/product/settings/custom-instructions", json={
                    "operation": "toggle", "id": "first", "enabled": False})
                entries = toggled.json()["settings_snapshot"]["custom_instructions"]
                assert [(entry["name"], entry["enabled"]) for entry in entries] == [("第一端名称", False), ("第二端名称", True)]
                deleted = await client.put("/api/v1/product/settings/custom-instructions", json={
                    "operation": "delete", "id": "first"})
                assert deleted.json()["settings_snapshot"]["custom_instructions"] == [entries[1]]
                invalid = await client.put("/api/v1/product/settings/custom-instructions", json={"operation": [], "id": "second"})
                assert invalid.status_code == 422
    asyncio.run(run())


def test_same_setting_waits_for_server_rollback_before_accepting_next_request(runtime):
    async def run():
        store = runtime.product_settings
        store.save_shadow({"experimental_context_gate_enabled": False})
        entered, release = asyncio.Event(), asyncio.Event()
        apply = runtime.set_shadow_context_gate_enabled
        calls = 0

        async def delayed_apply(enabled):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("第一条请求应用失败")
            return await apply(enabled)

        app = create_app(runtime, product_settings=store)
        with patch.object(runtime, "set_shadow_context_gate_enabled", side_effect=delayed_apply):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
                first = asyncio.create_task(client.put("/api/v1/product/settings/shadow", json={"experimental_context_gate_enabled": True}))
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(client.put("/api/v1/product/settings/shadow", json={"experimental_context_gate_enabled": True}))
                try:
                    await asyncio.sleep(.05)
                    assert not second.done()
                    assert calls == 1
                finally:
                    release.set()
                failed, saved = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert failed.status_code == 500 and saved.status_code == 200
        assert saved.json()["settings_snapshot"] == store.snapshot()
        assert runtime.config.shadow_agent.experimental_context_gate_enabled is True
        assert store.settings_only()["shadow"]["experimental_context_gate_enabled"] is True
    asyncio.run(run())


def test_notice_failure_does_not_turn_successful_server_commit_into_save_failure(runtime):
    async def run():
        store = runtime.product_settings
        app = create_app(runtime, product_settings=store)
        with patch.object(runtime.bus, "publish", side_effect=RuntimeError("模拟通知失败")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put("/api/v1/product/settings/shadow", json={"experimental_context_gate_enabled": True})
        assert response.status_code == 200
        assert response.json()["settings_snapshot"]["shadow"]["experimental_context_gate_enabled"] is True
        assert store.settings_only()["shadow"]["experimental_context_gate_enabled"] is True
        assert runtime.config.shadow_agent.experimental_context_gate_enabled is True
    asyncio.run(run())


@pytest.mark.parametrize("loading_fails", [False, True])
def test_wake_loading_or_rollback_preserves_concurrent_server_gate_value(runtime, loading_fails):
    async def run():
        store = runtime.product_settings
        entered, release = asyncio.Event(), asyncio.Event()

        async def load(*args, **kwargs):
            entered.set()
            await release.wait()
            if loading_fails:
                raise RuntimeError("模型加载失败")
            return SimpleNamespace(close=lambda: None)

        app = create_app(runtime, product_settings=store)
        with patch.object(store, "_compile_keyword"), patch.object(runtime, "_load_measured_model", side_effect=load):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                wake = asyncio.create_task(client.put("/api/v1/product/settings/wake", json={
                    "enabled": True, "keywords": ["hei claw"], "sensitivity": 75}))
                await asyncio.wait_for(entered.wait(), 5)
                try:
                    # 不同设置不等待模型加载，仍由服务端接受并确定最终值。
                    gate = await asyncio.wait_for(client.put("/api/v1/product/settings/shadow", json={"experimental_context_gate_enabled": True}), 5)
                    assert gate.status_code == 200
                    assert runtime.config.shadow_agent.experimental_context_gate_enabled is True
                    quiet = await asyncio.wait_for(client.put("/api/v1/product/settings/quiet-hours", json={
                        "enabled": False, "start": "22:00", "end": "07:00"}), 5)
                    assert quiet.status_code == 200
                finally:
                    release.set()
                finished = await asyncio.wait_for(wake, 5)
        assert finished.json()["pending"] is loading_fails
        assert runtime.config.shadow_agent.experimental_context_gate_enabled is True
        assert runtime.config.quiet_hours.start == "22:00"
        assert finished.json()["settings_snapshot"]["shadow"]["experimental_context_gate_enabled"] is True
    asyncio.run(run())


def test_persona_receipt_and_runtime_notice_share_server_revision_without_changing_episode(runtime):
    async def run():
        store = runtime.product_settings
        before = store.sync_state()
        frozen = deepcopy(runtime._shadow_user_profile)
        app = create_app(runtime, product_settings=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.put("/api/v1/product/settings/persona", json={"personality": "新人格", "user": "新资料"})
            body = result.json()
            snapshot = (await client.get("/api/v1/product/snapshot")).json()["settings"]
        assert body["effective_from"] == "next_session" and body["pending"] is True
        assert body["settings_snapshot"]["persona"] == snapshot["persona"] == {"personality": "新人格", "user": "新资料"}
        assert body["settings_snapshot"]["sync"] == snapshot["sync"] == store.sync_state()
        assert snapshot["sync"]["instance_id"] == before["instance_id"]
        assert snapshot["sync"]["revision"] == before["revision"] + 1
        assert runtime.snapshot().payload["product_settings"]["revision"] == snapshot["sync"]["revision"]
        assert runtime._shadow_user_profile == frozen
    asyncio.run(run())
