"""Phased startup（分阶段启动）：轻量入口、原生加载、依赖汇合和代理绑定。"""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from voice_terminal.config import AppConfig, PersonaConfig, DshConfig, WakeConfig
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.server.product import ProductSettingsStore


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_APP_PHASED_STARTUP", "1")
    config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"),
        dsh=replace(DshConfig(), home_dir=tmp_path / "dsh/home"), wake=WakeConfig(enabled=False))
    settings = ProductSettingsStore(path=tmp_path / "config/product-settings.json", config=config)
    return SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / "data")


def test_light_lifespan_restores_credentials_before_early_dsh_without_model_loading(monkeypatch):
    from apps.server import main as server
    events = []
    fake = SimpleNamespace(_application_phased_startup=True, startup_errors=[],
        config=AppConfig(), initialize=AsyncMock(), restore_startup_compute=AsyncMock(),
        restart_model_services=AsyncMock(),
        begin_dsh_start=lambda: events.append("dsh"),
        close=AsyncMock(), close_background_services=AsyncMock(), begin_application_shutdown=Mock(),
        dsh=SimpleNamespace(begin_start=lambda: events.append("dsh"), close=AsyncMock()))
    registry = Mock()
    async def credentials():
        events.append("credentials")
        return {"STARTUP_TEST_KEY": "isolated"}
    registry.runtime_fixed_credentials = credentials
    registry.scan = AsyncMock()
    monkeypatch.setenv("STARTUP_TEST_KEY", "")
    with patch.object(server, "SessionRuntime", return_value=fake), \
         patch.object(server, "ModelRegistryService", return_value=registry), \
         patch.object(server, "ProductSettingsStore", return_value=Mock(effective_config=Mock(return_value=AppConfig()))), \
         patch.object(server, "_runtime_build_info", return_value={}), patch.object(server, "application_report"):
        app, _ = server.build_application()
        async def run():
            async with app.router.lifespan_context(app):
                assert events == ["credentials", "dsh"]
                assert os.environ["STARTUP_TEST_KEY"] == "isolated"
                fake.initialize.assert_not_awaited()
                fake.restore_startup_compute.assert_not_awaited()
        asyncio.run(run())
    fake.begin_application_shutdown.assert_called_once()
    fake.close.assert_awaited_once()
    fake.dsh.close.assert_awaited_once()


def test_local_and_session_controls_are_idempotent_and_require_local_completion(runtime):
    async def run():
        first = await runtime.application_control("initialize-session")
        assert not first["ok"]
        async def local():
            await asyncio.sleep(.01)
            runtime._application_local_ready = True
        async def session():
            runtime._application_session_ready = True
            for key in ("audio", "asr", "llm", "tts"):
                runtime.state.set_service(key, "READY")
        with patch.object(runtime, "initialize_local", new=AsyncMock(side_effect=local)) as load, \
             patch.object(runtime, "initialize_session", new=AsyncMock(side_effect=session)) as join, \
             patch.object(runtime, "restore_startup_compute", new=AsyncMock()):
            assert all(item["ok"] for item in await asyncio.gather(*(
                runtime.application_control("initialize-local") for _ in range(2))))
            assert all(item["ok"] for item in await asyncio.gather(*(
                runtime.application_control("initialize-session") for _ in range(2))))
            load.assert_awaited_once()
            join.assert_awaited_once()
    asyncio.run(run())


def test_early_bridge_does_not_bind_or_report_ready_until_session_exists(runtime):
    runtime.dsh._state = "ready"
    runtime.dsh._process = SimpleNamespace(pid=123, returncode=None)
    reconnect = AsyncMock()
    runtime.shadow_session = SimpleNamespace(reconnect=reconnect)
    async def run():
        pending = await runtime.application_delegated_status()
        assert pending["dsh"]["state"] == "waiting"
        assert "等待语音会话绑定" in pending["dsh"]["phase"]
        reconnect.assert_not_awaited()
        assert getattr(runtime, "_application_bound_dsh_pid", None) is None
        runtime._application_session_ready = True
        assert (await runtime.application_delegated_status())["dsh"]["state"] == "ready"
        await runtime.application_delegated_status()
        reconnect.assert_awaited_once()
    asyncio.run(run())


def test_loading_speaker_reports_startup_phase_without_an_unavailable_error(runtime):
    runtime.state.set_service("speaker", "STARTING")
    speaker = runtime.application_component_status()["speaker"]
    assert speaker["state"] == "starting"
    assert speaker["phase"] == "加载声纹工作进程"
    assert speaker["error"] == ""


def test_binding_failure_is_reported_and_start_does_not_claim_full_readiness(runtime):
    runtime.dsh._state = "ready"
    runtime.dsh._process = SimpleNamespace(pid=123, returncode=None)
    runtime._application_session_ready = True
    runtime.shadow_session = SimpleNamespace(reconnect=AsyncMock(side_effect=RuntimeError("unavailable")))
    async def run():
        with patch.object(runtime.dsh, "begin_start"), patch.object(runtime.dsh, "wait_ready", new=AsyncMock(return_value=True)):
            result = await runtime.application_control("dsh-start")
            assert not result["ok"] and "重新绑定失败" in result["message"]
    asyncio.run(run())


@pytest.mark.parametrize("shutdown", [False, True])
def test_native_load_leaves_event_loop_responsive_and_cleans_late_result_on_cancel(runtime, shutdown):
    entered, release = Event(), Event()
    result = SimpleNamespace(close=Mock())
    def native():
        entered.set()
        assert release.wait(5)
        return result
    async def run():
        task = asyncio.create_task(runtime._load_native(native))
        try:
            while not entered.is_set():
                await asyncio.sleep(.01)
            # 健康与设置请求所在循环仍能响应，取消也不能遗留新建的原生线程。
            await asyncio.wait_for(asyncio.sleep(.01), .2)
            if shutdown:
                runtime.begin_application_shutdown()
            else:
                task.cancel()
            await asyncio.sleep(.01)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            result.close.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_wait_for_early_dsh_preserves_failure_without_implicit_second_start(runtime):
    runtime.dsh._state = "unavailable"
    runtime.dsh._message = "模拟首次启动失败"
    async def run():
        with patch.object(runtime.dsh, "begin_start") as start, \
             patch.object(runtime.dsh, "wait_ready", new=AsyncMock(return_value=False)):
            assert not (await runtime.application_control("dsh-wait"))["ok"]
            start.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("percent", [80.0, 94.9, None])
def test_dsh_starts_in_parallel_below_95_even_when_model_resource_budget_would_block(runtime, percent):
    async def run():
        with patch("voice_terminal.application.dsh_startup.startup_memory_percent", return_value=percent), \
             patch("voice_terminal.application.startup_budget.sample_startup_resources", side_effect=AssertionError("代理不受模型资源预算影响")), \
             patch.object(runtime.dsh, "start", new=AsyncMock()) as start:
            runtime.begin_dsh_start()
            await asyncio.wait_for(runtime.dsh._start_task, .5)
            start.assert_awaited_once()
            assert not runtime.dsh._waiting_for_memory
    asyncio.run(run())


def test_dsh_waits_at_95_then_resumes_without_duplicate_start(runtime):
    percent = [95.0]
    async def run():
        with patch("voice_terminal.application.dsh_startup.startup_memory_percent", side_effect=lambda: percent[0]), \
             patch.object(runtime.dsh, "start", new=AsyncMock()) as start:
            runtime.begin_dsh_start()
            original = runtime.dsh._start_task
            await asyncio.sleep(.02)
            assert runtime.dsh._waiting_for_memory
            assert runtime.application_component_status()["dsh"]["state"] == "waiting"
            start.assert_not_awaited()
            runtime.begin_dsh_start()
            assert runtime.dsh._start_task is original
            percent[0] = 94.9
            await asyncio.wait_for(original, 1.5)
            start.assert_awaited_once()
            assert not runtime.dsh._waiting_for_memory
    asyncio.run(run())


def test_exit_during_dsh_memory_wait_never_launches_process(runtime):
    async def run():
        with patch("voice_terminal.application.dsh_startup.startup_memory_percent", return_value=99), \
             patch.object(runtime.dsh, "start", new=AsyncMock()) as start:
            runtime.begin_dsh_start()
            await asyncio.sleep(.02)
            assert runtime.dsh._waiting_for_memory
            runtime.begin_application_shutdown()
            await runtime.dsh.close()
            start.assert_not_awaited()
            assert not runtime.dsh._waiting_for_memory
    asyncio.run(run())


def test_local_loader_does_not_connect_asr_or_create_session(runtime):
    fake_vad = SimpleNamespace(backend="onnx", device="cpu", load_error=None)
    fake_audio = Mock(enabled=False, status="DISABLED", error="")
    async def run():
        with patch("voice_terminal.audio.levels.AudioLevelMeter"), \
             patch("voice_terminal.server.websocket.AudioCapture"), \
             patch("voice_terminal.server.websocket.AudioPreprocessor", return_value=fake_audio), \
             patch("voice_terminal.server.websocket.FsmnVadDetector", return_value=fake_vad) as vad, \
             patch("voice_terminal.server.websocket.AsrWorkerClient") as asr, \
             patch.object(runtime, "_restore_or_persist_voice_identity"), \
             patch.object(runtime, "_initialize_speaker", new=AsyncMock()):
            await runtime.initialize_local()
            assert runtime._application_local_ready and not runtime._application_session_ready
            assert runtime.controller is None
            asr.assert_not_called()
            vad.assert_called_once()
    asyncio.run(run())


def test_loading_api_allows_settings_but_blocks_recording_and_validates_flags_atomically(runtime, monkeypatch):
    monkeypatch.setenv("VOICE_APP_INTERNAL_TOKEN", "startup-test")
    monkeypatch.setenv("VOICE_APP_ASR_AVAILABLE", "0")
    monkeypatch.setenv("VOICE_APP_ANALYSIS_AVAILABLE", "0")
    monkeypatch.setenv("VOICE_APP_ASR_MANAGED", "0")
    monkeypatch.setenv("VOICE_APP_ANALYSIS_MANAGED", "0")
    app = create_app(runtime, product_settings=runtime.product_settings)
    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            snapshot = await client.get("/api/v1/product/snapshot")
            assert snapshot.status_code == 200
            saved = await client.put("/api/v1/product/settings/speaker", json={"mode": "off", "owner_only_barge_in": False,
                "confirm_threshold": .6, "continuity_threshold": .4})
            assert saved.status_code == 200 and saved.json()["pending"]
            with patch.object(runtime, "capture_speaker_test", new=AsyncMock()) as capture:
                blocked = await client.post("/api/v1/speaker/test/capture", json={"seconds": 1})
                assert blocked.status_code == 503
                capture.assert_not_awaited()
            invalid = await client.post("/internal/application/control", headers={"Authorization": "Bearer startup-test"},
                json={"action": "initialize-session", "asr_available": True, "analysis_available": "invalid",
                      "asr_managed": True, "analysis_managed": True})
            assert invalid.status_code == 422
            assert os.environ["VOICE_APP_ASR_AVAILABLE"] == os.environ["VOICE_APP_ANALYSIS_AVAILABLE"] == "0"
            assert os.environ["VOICE_APP_ASR_MANAGED"] == os.environ["VOICE_APP_ANALYSIS_MANAGED"] == "0"
            denied = await client.post("/internal/application/control", json={"action": "initialize-local"})
            assert denied.status_code == 403
    asyncio.run(run())


def test_loading_api_refreshes_live_worker_ownership_before_session_initialization(runtime, monkeypatch):
    monkeypatch.setenv("VOICE_APP_INTERNAL_TOKEN", "startup-test")
    for name in ("VOICE_APP_ASR_AVAILABLE", "VOICE_APP_ANALYSIS_AVAILABLE",
                 "VOICE_APP_ASR_MANAGED", "VOICE_APP_ANALYSIS_MANAGED"):
        monkeypatch.setenv(name, "0")
    app = create_app(runtime, product_settings=runtime.product_settings)

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.object(runtime, "application_control", new=AsyncMock(return_value={"ok": True})):
                response = await client.post("/internal/application/control",
                    headers={"Authorization": "Bearer startup-test"},
                    json={"action": "initialize-session", "asr_available": True,
                          "analysis_available": True, "asr_managed": True,
                          "analysis_managed": False})
            assert response.status_code == 200
            assert os.environ["VOICE_APP_ASR_AVAILABLE"] == "1"
            assert os.environ["VOICE_APP_ANALYSIS_AVAILABLE"] == "1"
            assert os.environ["VOICE_APP_ASR_MANAGED"] == "1"
            assert os.environ["VOICE_APP_ANALYSIS_MANAGED"] == "0"

    asyncio.run(run())


def test_direct_voice_start_and_retries_do_not_bypass_loading_or_shutdown(runtime):
    async def run():
        with patch.object(runtime, "_apply_pending_product_settings", new=AsyncMock()) as apply, \
             patch.object(runtime, "initialize", new=AsyncMock()) as load:
            await runtime.start_listening()
            apply.assert_not_awaited()
            assert not (await runtime.application_control("reinitialize"))["ok"]
            load.assert_not_awaited()
            runtime.begin_application_shutdown()
            assert not (await runtime.application_control("initialize-local"))["ok"]
            with patch.object(runtime, "_connect_speaker_worker", new=AsyncMock()) as connect:
                assert not await runtime.ensure_speaker_worker()
                connect.assert_not_awaited()
            with pytest.raises(RuntimeError, match="退出"):
                await runtime._start_managed_speaker_worker()
    asyncio.run(run())


def test_reinitialize_reports_asr_session_when_other_voice_core_services_are_missing(runtime):
    """本地 ASR 会话可恢复时，不把缺少 LLM/TTS 凭据误作工作进程重连失败。"""
    async def initialize_with_missing_remote_services():
        for name, status in (("audio", "READY"), ("asr", "READY"),
                             ("llm", "ERROR"), ("tts", "ERROR")):
            runtime.state.set_service(name, status)

    async def run():
        runtime._application_local_ready = True
        runtime._application_session_ready = True
        with patch.object(runtime, "close", new=AsyncMock()), \
             patch.object(runtime, "initialize", new=AsyncMock(side_effect=initialize_with_missing_remote_services)), \
             patch.object(runtime, "snapshot", return_value={}):
            response = await runtime.application_control("reinitialize", endpoint_table={})

        assert response["ok"] is False
        assert response["session_initialized"] is True
        assert response["asr_ready"] is True
        assert response["message"]
        services = runtime.health_payload()["services"]
        assert services["llm"] == "ERROR"
        assert services["tts"] == "ERROR"

    asyncio.run(run())
