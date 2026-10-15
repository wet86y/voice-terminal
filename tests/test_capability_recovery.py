"""Capability recovery（能力级恢复）：只重建一个能力，不重启语音服务进程。"""
import asyncio
from dataclasses import replace

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


def _ready(runtime, *names):
    for name in names:
        runtime.state.set_service(name, "READY")


def _session_ready(runtime):
    """会话初始化已完成：能力级恢复只在非初始化期可用。"""
    runtime._application_session_ready = True
    runtime.state.set_service("kws", "DISABLED")


def test_capability_rows_report_loading_failure_and_disabled(runtime):
    _session_ready(runtime)
    _ready(runtime, "audio", "asr", "llm")
    runtime.state.set_service("tts", "STARTING")
    runtime._record_error("vad", "检测器加载失败", status_service="asr")
    rows = {row["id"]: row for row in runtime.capability_rows()}
    assert rows["tts"]["state"] == "STARTING"
    assert rows["tts"]["blocking"] is True
    assert rows["tts"]["retry"] == "tts"
    assert rows["tts"]["error"] == ""
    # 子环节错误归属同一能力：VAD 失败直接呈现在识别能力行上。
    assert rows["asr"]["error"] == "检测器加载失败"
    # 唤醒按配置停用时不进入阻塞能力，也不提供恢复入口。
    assert rows["kws"]["state"] == "DISABLED"
    assert rows["kws"]["blocking"] is False
    assert rows["kws"]["retry"] is None


def test_retry_capability_rebuilds_only_that_capability(runtime):
    async def run():
        _session_ready(runtime)
        _ready(runtime, "audio", "asr", "llm")
        runtime.state.set_service("tts", "ERROR")
        runtime._record_error("tts", "模型文件缺失")
        calls = []

        async def repair():
            calls.append("tts")
            runtime.state.set_service("tts", "READY")

        runtime._CAPABILITY_RECOVERY = {**SessionRuntime._CAPABILITY_RECOVERY, "tts": "_repair_tts"}
        runtime._repair_tts = repair
        before = dict(runtime.state.services)
        result = await runtime.retry_capability("tts")
        assert result["ok"] is True
        assert calls == ["tts"]
        assert runtime.state.services["tts"] == "READY"
        assert runtime._service_errors.get("tts") is None
        # 其余能力的状态没有被这次恢复改动。
        assert {key: value for key, value in runtime.state.services.items() if key != "tts"} == \
            {key: value for key, value in before.items() if key != "tts"}
        rows = {row["id"]: row for row in runtime.capability_rows()}
        assert rows["tts"]["state"] == "READY"
        assert rows["tts"]["retry"] is None
    asyncio.run(run())


def test_retry_capability_keeps_failure_reason_when_repair_fails(runtime):
    async def run():
        _session_ready(runtime)
        _ready(runtime, "audio", "asr", "llm")
        runtime.state.set_service("tts", "STARTING")

        async def broken():
            raise RuntimeError("原生程序启动失败")

        runtime._CAPABILITY_RECOVERY = {**SessionRuntime._CAPABILITY_RECOVERY, "tts": "_broken_tts"}
        runtime._broken_tts = broken
        result = await runtime.retry_capability("tts")
        assert result["ok"] is False
        rows = {row["id"]: row for row in runtime.capability_rows()}
        assert rows["tts"]["state"] == "ERROR"
        assert "恢复失败" in rows["tts"]["error"]
        assert rows["tts"]["retry"] == "tts"
    asyncio.run(run())


def test_retry_capability_rejects_unknown_or_stopping(runtime):
    async def run():
        assert (await runtime.retry_capability("nope"))["ok"] is False
        runtime._application_stopping = True
        assert (await runtime.retry_capability("tts"))["ok"] is False
    asyncio.run(run())


def test_application_control_routes_capability_retry(runtime):
    async def run():
        _session_ready(runtime)
        _ready(runtime, "audio", "asr", "llm")
        runtime.state.set_service("tts", "ERROR")
        seen = []

        async def repair():
            seen.append("tts")
            runtime.state.set_service("tts", "READY")

        runtime._CAPABILITY_RECOVERY = {**SessionRuntime._CAPABILITY_RECOVERY, "tts": "_repair_tts"}
        runtime._repair_tts = repair
        response = await runtime.application_control("capability-retry", capability="tts")
        assert response["ok"] is True and seen == ["tts"]
        rejected = await runtime.application_control("capability-retry", capability="unknown")
        assert rejected["ok"] is False
    asyncio.run(run())


def test_status_payload_exposes_capability_rows(runtime):
    _session_ready(runtime)
    _ready(runtime, "audio", "asr", "llm", "tts")
    payload = runtime.application_component_status()
    assert {row["id"] for row in payload["capabilities"]} >= {"audio", "asr", "llm", "tts"}
    assert all(row["blocking"] for row in payload["capabilities"] if row["id"] in {"audio", "asr", "llm", "tts"})


def test_health_payload_carries_can_start_reason(runtime):
    _session_ready(runtime)
    _ready(runtime, "audio", "asr", "llm")
    runtime.state.set_service("tts", "STARTING")
    payload = runtime.health_payload()
    assert payload["can_start"] is False
    assert payload["can_start_reason"]["code"] == "CAPABILITY_LOADING"
    assert payload["can_start_reason"]["services"] == ["tts"]
