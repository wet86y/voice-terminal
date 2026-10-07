from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import os
from unittest.mock import patch

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app


def test_manual_create_requires_host_credential_and_fixes_business_fields(tmp_path: Path):
    async def scenario():
        config = AppConfig(model_dir=tmp_path / 'models', persona=PersonaConfig(directory=tmp_path / 'persona'))
        settings = ProductSettingsStore(path=tmp_path / 'settings.json', config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / '.runtime')
        calls = []
        async def task_api(method, path, payload=None):
            calls.append((method, path, payload))
            return {'task_id': 'manual-test', **payload}
        runtime.dsh.bridge.task_api = task_api
        transport = httpx.ASGITransport(app=create_app(runtime, product_settings=settings), client=('127.0.0.1', 123))
        with patch.dict(os.environ, {'VOICE_APP_INTERNAL_TOKEN': 'test-host-secret'}):
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
                route = '/internal/application/wake-tasks'
                value = {'text': '检查真实任务', 'at': '2030-01-01T00:00:00Z'}
                assert (await client.post(route, json=value)).status_code == 403
                headers = {'Authorization': 'Bearer test-host-secret'}
                for extra in ({'target': 'both'}, {'creation_source': 'agent'}, {'reference': '伪造背景'}, {'daily': {}}):
                    assert (await client.post(route, headers=headers, json={**value, **extra})).status_code == 422
                for rule in ({'at': value['at']}, {'daily': {'time': '09:00:00', 'time_zone': 'Asia/Hong_Kong'}},
                             {'weekly': {'time': '09:00:00', 'time_zone': 'Asia/Hong_Kong', 'weekdays': [1, 3]}}):
                    result = await client.post(route, headers=headers, json={'text': ' 检查真实任务 ', **rule})
                    assert result.status_code == 200
                    assert result.json()['creation_source'] == 'manual'
                    assert calls[-1][2] == {'text': '检查真实任务', **rule, 'target': 'shadow_agent', 'reference': '', 'creation_source': 'manual'}
        await runtime.close_background_services()
        await runtime.dsh.close()
    asyncio.run(scenario())


def test_archive_delete_rejects_cancel_receipt_instead_of_reporting_success(tmp_path: Path):
    async def scenario():
        config = AppConfig(model_dir=tmp_path / 'models', persona=PersonaConfig(directory=tmp_path / 'persona'))
        settings = ProductSettingsStore(path=tmp_path / 'settings.json', config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / '.runtime')
        calls = []
        async def task_api(method, path, payload=None):
            calls.append((method, path))
            return {'task_id': 'old-plugin', 'status': 'cancelled'}
        runtime.dsh.bridge.task_api = task_api
        transport = httpx.ASGITransport(app=create_app(runtime, product_settings=settings))
        try:
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                result = await client.delete('/api/v1/wake-tasks/old-plugin')
                assert result.status_code == 409
                assert calls == [('DELETE', 'tasks/old-plugin/archive')]
        finally:
            await runtime.close_background_services()
            await runtime.dsh.close()
    asyncio.run(scenario())


def test_wake_task_routes_cover_dsh_tools_and_settings_page(tmp_path: Path):
    async def scenario() -> None:
        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "persona"),
        )
        settings = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / ".runtime")
        catalog: dict[str, dict] = {}
        frozen_reference = ""

        async def task_api(method: str, path: str, payload: dict | None = None) -> dict:
            nonlocal frozen_reference
            if method == "GET" and path == "tasks":
                return {"tasks": list(catalog.values())}
            if method == "POST" and path == "tasks":
                row = {"task_id": "schedule-test", "status": "pending", "has_reference": True,
                       **(payload or {})}
                frozen_reference = row.pop("reference", "")
                catalog[row["task_id"]] = row
                return row
            if path.endswith("/reference"):
                return {"task_id": "schedule-test", "reference": frozen_reference}
            if method == "DELETE" and path.endswith("/archive"):
                catalog.pop("schedule-test")
                return {"task_id": "schedule-test", "deleted": True}
            row = catalog["schedule-test"]
            if method == "PATCH":
                row.update(payload or {})
            elif method == "DELETE":
                row["status"] = "cancelled"
            return row

        runtime.dsh.bridge.task_api = task_api
        app = create_app(runtime, product_settings=settings)
        auth = {"Authorization": f"Bearer {runtime.dsh._bridge_token}"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.post(
                "/api/internal/wake-tasks",
                json={"target": "voice_llm", "when": "30m", "text": "检查"},
            )
            assert unauthorized.status_code == 401

            created_response = await client.post(
                "/api/internal/wake-tasks",
                headers=auth,
                json={
                    "target": "shadow_agent", "when": "30m", "text": "检查任务",
                    "reference": '[{"role":"user","content":"被冻结的上下文"}]',
                },
            )
            assert created_response.status_code == 200
            task_id = created_response.json()["task_id"]

            listed = await client.get("/api/v1/wake-tasks")
            assert listed.status_code == 200
            assert listed.json()["tasks"][0]["task_id"] == task_id
            assert listed.json()["tasks"][0]["has_reference"] is True

            updated = await client.patch(
                f"/api/v1/wake-tasks/{task_id}",
                json={"target": "both", "text": "更新后的任务"},
            )
            assert updated.status_code == 200
            assert updated.json()["target"] == "both"
            assert updated.json()["text"] == "更新后的任务"

            reference = await client.get(
                f"/api/internal/wake-tasks/{task_id}/reference", headers=auth,
            )
            assert reference.status_code == 200
            assert "被冻结的上下文" in reference.json()["reference"]

            cancelled = await client.post(
                f"/api/internal/wake-tasks/{task_id}/cancel", headers=auth,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

            cancelled_public = await client.post(f"/api/v1/wake-tasks/{task_id}/cancel")
            assert cancelled_public.status_code == 200
            assert cancelled_public.json()["status"] == "cancelled"
            assert len((await client.get("/api/v1/wake-tasks")).json()["tasks"]) == 1

            removed = await client.delete(f"/api/v1/wake-tasks/{task_id}")
            assert removed.status_code == 200
            assert removed.json() == {"deleted": True}
            assert (await client.get("/api/v1/wake-tasks")).json()["tasks"] == []

        await runtime.close_background_services()
        await runtime.dsh.close()

    asyncio.run(scenario())
