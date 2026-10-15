"""Listening capability gate（监听能力门禁）：能力加载中拦截主监听并显示启动中。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.config import AppConfig, DshConfig, PersonaConfig, WakeConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_APP_PHASED_STARTUP", "1")
    config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"),
        dsh=replace(DshConfig(), home_dir=tmp_path / "dsh/home"), wake=WakeConfig(enabled=False))
    settings = ProductSettingsStore(path=tmp_path / "config/product-settings.json", config=config)
    return SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / "data")


def _fake_controller():
    """会话控制器替身：只保留监听开关与快照所需的空引用，避免异步替身产生多余协程。"""
    return SimpleNamespace(start_listening=AsyncMock(), stop_listening=AsyncMock(),
                           episode=None, output=None)


def _prepare(runtime, **services):
    runtime._application_session_ready = True
    for name in ("audio", "kws", "asr", "llm", "tts"):
        runtime.state.set_service(name, "DISABLED" if name == "kws" else "READY")
    for name, value in services.items():
        runtime.state.set_service(name, value)


async def _drain(runtime):
    """读取总线上的公开事件：门禁只发布状态事件，不发布错误事件。"""
    events = []
    subscription = runtime.bus.subscribe()
    async def consume():
        async for value in subscription:
            events.append(value)
    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    return events, task


def test_loading_capability_blocks_listening_with_status_not_error(runtime):
    async def run():
        _prepare(runtime, tts="STARTING")
        runtime.controller = _fake_controller()
        events, task = await _drain(runtime)
        try:
            await runtime.start_listening()
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime.controller.start_listening.assert_not_awaited()
        assert runtime._listen_resume_requested is True
        codes = [value.payload.get("code") for value in events if value.type == "status"]
        assert codes == ["CAPABILITY_LOADING"]
        assert not [value for value in events if value.type == "error"]
        message = next(value.payload["message"] for value in events if value.type == "status")
        assert "TTS（语音合成）" in message and "自动开始监听" in message
    asyncio.run(run())


def test_failed_capability_keeps_existing_error_path(runtime):
    async def run():
        _prepare(runtime, tts="ERROR")
        runtime.controller = _fake_controller()
        await runtime.start_listening()
        # 真实失败仍交给会话控制器报告，不在这里吞掉。
        runtime.controller.start_listening.assert_awaited()
        assert not runtime._listen_resume_requested
    asyncio.run(run())


def test_user_stop_cancels_pending_resume_intent(runtime):
    async def run():
        _prepare(runtime, tts="STARTING")
        runtime.controller = _fake_controller()
        await runtime.start_listening()
        assert runtime._listen_resume_requested is True
        await runtime.stop_listening()
        assert runtime._listen_resume_requested is False
        runtime.controller.stop_listening.assert_awaited()
    asyncio.run(run())


def test_capability_recovery_starts_listening_after_loading_finishes(runtime):
    async def run():
        _prepare(runtime, tts="STARTING")
        runtime.controller = _fake_controller()
        await runtime.start_listening()
        assert runtime._listen_resume_requested is True

        async def repair():
            runtime.state.set_service("tts", "READY")

        runtime._CAPABILITY_RECOVERY = {**SessionRuntime._CAPABILITY_RECOVERY, "tts": "_repair_tts"}
        runtime._repair_tts = repair
        result = await runtime.retry_capability("tts")
        assert result["ok"] is True
        # 能力恢复完成后自动补上用户此前被拦截的开始意图。
        runtime.controller.start_listening.assert_awaited()
        assert runtime._listen_resume_requested is None
    asyncio.run(run())


def test_ready_capabilities_start_listening_directly(runtime):
    async def run():
        _prepare(runtime)
        runtime.controller = _fake_controller()
        await runtime.start_listening()
        runtime.controller.start_listening.assert_awaited_once()
        assert runtime._listen_resume_requested is None
    asyncio.run(run())
