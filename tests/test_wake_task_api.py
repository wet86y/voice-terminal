from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.wake_tasks import ScheduledWakeManager


def test_wake_task_routes_cover_dsh_tools_and_settings_page(tmp_path: Path):
    async def no_delivery(_task):
        return False

    async def scenario() -> None:
        config = AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "persona"),
        )
        settings = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings)
        runtime.wake_tasks = ScheduledWakeManager(
            tmp_path / "wake-tasks.json", deliver_voice=no_delivery, deliver_shadow=no_delivery,
        )
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
            assert "reference" not in listed.json()["tasks"][0]

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

            removed = await client.delete(f"/api/v1/wake-tasks/{task_id}")
            assert removed.status_code == 200
            assert (await client.get("/api/v1/wake-tasks")).json()["tasks"] == []

        await runtime.close_background_services()
        await runtime.dsh.close()

    asyncio.run(scenario())
