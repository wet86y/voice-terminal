from __future__ import annotations

import asyncio
import threading
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from voice_terminal.audio_enhancements import AudioEnhancements
from voice_terminal.config import AppConfig, ComputeConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.settings_runtime import ProductSettingsRuntimeMixin


def model_status(device: str, *, requested="cpu"):
    return {
        "status": "READY", "model_loaded": True, "model_warmed": True,
        "device": device, "requested_device": requested,
        "offline": {"status": "DISABLED", "model_loaded": False, "model_warmed": False},
    }


def make_runtime(tmp_path: Path, *, initial_listening=True):
    devices = {"paraformer": "cpu", "emotion": "cpu", "voice_vad": "cpu",
               "kws": "cpu", "smart_turn": "cpu", "campplus": "cpu"}
    config = replace(AppConfig(model_dir=tmp_path / "models"),
                     compute=ComputeConfig(devices=devices),
                     audio_enhancements=AudioEnhancements(dual_pass=False))
    store = ProductSettingsStore(path=tmp_path / "config/product-settings.json", config=config)
    store.save_compute({"devices": devices})
    runtime = ProductSettingsRuntimeMixin()
    runtime.config = config
    runtime.product_settings = store
    runtime._project_root = tmp_path
    runtime._session_lock = asyncio.Lock()
    runtime._closed = False
    runtime._application_stopping = False
    runtime.state = SimpleNamespace(listening=initial_listening)
    runtime.bus = SimpleNamespace(publish=AsyncMock())
    runtime.snapshot = lambda: {}

    async def stop_listening():
        runtime.state.listening = False

    async def start_listening():
        runtime.state.listening = True

    runtime.controller = SimpleNamespace(stop_listening=AsyncMock(side_effect=stop_listening))
    runtime.start_listening = AsyncMock(side_effect=start_listening)
    runtime.compute_status = AsyncMock(return_value={
        "models": {"paraformer": {"service": "READY", "model_loaded": True,
                                    "model_warmed": True, "device": "cpu"}},
        "auxiliary": {"dual_pass": {"service": "DISABLED"}},
    })
    return runtime


def gpu_preflight():
    return (
        patch("voice_terminal.server.websocket.hardware_status",
              return_value={"models": {"paraformer": {"gpu_available": True}}}),
        patch("voice_terminal.compute.model_hardware_status", side_effect=lambda value, *_args, **_kwargs: value),
        patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}),
    )


@pytest.mark.asyncio
async def test_compute_commit_preserves_settings_applied_during_save(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(return_value=model_status("cuda", requested="auto")))
    saved = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = runtime.product_settings.save_compute_model

    def delayed_save(*args, **kwargs):
        result = original_save(*args, **kwargs)
        loop.call_soon_threadsafe(saved.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr(runtime.product_settings, "save_compute_model", delayed_save)
    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        task = asyncio.create_task(runtime.switch_compute_device({"model_id": "paraformer", "device": "auto"}))
        try:
            await asyncio.wait_for(saved.wait(), 5)
            await asyncio.to_thread(runtime.product_settings.save_speaker,
                                    {"mode": "observe", "confirm_threshold": 0.6, "owner_only_barge_in": False})
            runtime.config = replace(runtime.config, speaker=replace(runtime.config.speaker, mode="observe"))
        finally:
            release.set()
        await task
    assert runtime.config.speaker.mode == "observe"
    assert runtime.config.compute.devices["paraformer"] == "auto"
    assert runtime.product_settings.settings_only()["speaker"]["mode"] == "observe"


@pytest.mark.asyncio
async def test_auto_switch_uses_actual_device_and_persists_only_after_verification(tmp_path):
    runtime = make_runtime(tmp_path)
    target = model_status("cuda", requested="auto")
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(return_value=target))
    runtime.compute_status.return_value["models"]["paraformer"]["requested_device"] = "cpu"

    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        saved = await runtime.switch_compute_device({"model_id": "paraformer", "device": "auto"})

    runtime.asr.configure_compute.assert_awaited_once_with("auto", allow_cuda_fallback=False)
    assert saved["devices"]["paraformer"] == "auto"
    assert saved["fallback_devices"]["paraformer"] == "cuda"
    assert runtime.config.compute.devices["paraformer"] == "auto"
    assert runtime.state.listening is True
    runtime.controller.stop_listening.assert_awaited_once()
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_cpu_to_cuda_switch_commits_only_target_after_verification(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(
        return_value=model_status("cuda", requested="cuda")))

    with gpu_preflight()[0], gpu_preflight()[1], gpu_preflight()[2]:
        saved = await runtime.switch_compute_device({"model_id": "paraformer", "device": "cuda"})

    runtime.asr.configure_compute.assert_awaited_once_with("cuda", allow_cuda_fallback=False)
    assert saved["devices"]["paraformer"] == "cuda"
    assert saved["fallback_devices"]["paraformer"] == "cuda"
    assert runtime.config.compute.devices["paraformer"] == "cuda"
    assert runtime.state.listening is True
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_cuda_to_cpu_switch_keeps_unlistened_state(tmp_path):
    runtime = make_runtime(tmp_path, initial_listening=False)
    devices = {**runtime.config.compute.devices, "paraformer": "cuda"}
    runtime.config = replace(runtime.config, compute=replace(runtime.config.compute, devices=devices))
    runtime.product_settings.save_compute({"devices": devices})
    runtime.compute_status.return_value["models"]["paraformer"].update(
        device="cuda", requested_device="cuda")
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(
        return_value=model_status("cpu", requested="cpu")))

    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        saved = await runtime.switch_compute_device({"model_id": "paraformer", "device": "cpu"})

    assert saved["devices"]["paraformer"] == "cpu"
    assert saved["fallback_devices"]["paraformer"] == "cpu"
    runtime.controller.stop_listening.assert_not_awaited()
    runtime.start_listening.assert_not_awaited()
    assert runtime.state.listening is False


@pytest.mark.asyncio
async def test_explicit_cuda_cpu_fallback_is_rejected_then_old_model_and_listener_are_restored(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        model_status("cpu", requested="cuda"), model_status("cpu"),
    ]))
    before = runtime.product_settings.snapshot()["compute"]

    with gpu_preflight()[0], gpu_preflight()[1], gpu_preflight()[2]:
        with pytest.raises(RuntimeError, match="请求 cuda.*实际运行于 cpu"):
            await runtime.switch_compute_device({"model_id": "paraformer", "device": "cuda"})

    assert [call.args[0] for call in runtime.asr.configure_compute.await_args_list] == ["cuda", "cpu"]
    assert runtime.product_settings.snapshot()["compute"] == before
    assert runtime.state.listening is True
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_failure_restores_old_model_and_settings(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        model_status("cuda", requested="cuda"), model_status("cpu"),
    ]))
    before = runtime.product_settings.snapshot()["compute"]

    with gpu_preflight()[0], gpu_preflight()[1], gpu_preflight()[2], \
            patch.object(runtime.product_settings, "_save_settings", side_effect=OSError("settings disk full")):
        with pytest.raises(OSError, match="settings disk full"):
            await runtime.switch_compute_device({"model_id": "paraformer", "device": "cuda"})

    assert [call.args[0] for call in runtime.asr.configure_compute.await_args_list] == ["cuda", "cpu"]
    assert runtime.product_settings.snapshot()["compute"] == before
    assert runtime.config.compute.devices["paraformer"] == "cpu"
    assert runtime.state.listening is True
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_can_restore_cpu_fallback_under_old_cuda_preference(tmp_path):
    runtime = make_runtime(tmp_path)
    devices = {**runtime.config.compute.devices, "paraformer": "cuda"}
    runtime.config = replace(runtime.config, compute=replace(runtime.config.compute, devices=devices))
    runtime.product_settings.save_compute({"devices": devices})
    runtime.compute_status.return_value["models"]["paraformer"]["device"] = "cpu"
    runtime.compute_status.return_value["models"]["paraformer"]["requested_device"] = "cuda"
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        RuntimeError("target load failed"), model_status("cpu", requested="cuda"),
    ]))

    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        with pytest.raises(RuntimeError, match="target load failed"):
            await runtime.switch_compute_device({"model_id": "paraformer", "device": "cpu"})

    assert runtime.asr.configure_compute.await_args_list == [
        call("cpu", allow_cuda_fallback=False), call("cuda", allow_cuda_fallback=True),
    ]
    assert runtime.product_settings.snapshot()["compute"]["devices"]["paraformer"] == "cuda"
    assert runtime.state.listening is True
    runtime.start_listening.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_failure_keeps_listener_stopped_and_reports_both_failures(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        RuntimeError("target load failed"), RuntimeError("old model restore failed"),
    ]))

    with gpu_preflight()[0], gpu_preflight()[1], gpu_preflight()[2]:
        with pytest.raises(RuntimeError, match="恢复原模型失败.*old model restore failed"):
            await runtime.switch_compute_device({"model_id": "paraformer", "device": "cuda"})

    assert runtime.state.listening is False
    runtime.start_listening.assert_not_awaited()
    assert runtime.product_settings.snapshot()["compute"]["devices"]["paraformer"] == "cpu"


@pytest.mark.asyncio
@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize("already_written", [False, True])
async def test_cancel_during_device_save_waits_for_disk_and_restores_only_target(
        tmp_path, monkeypatch, close, already_written):
    """落盘前后取消都必须等待写线程；其他模型的并发保存仍保留。"""
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        model_status("cuda", requested="auto"), model_status("cpu"),
    ]))
    before = runtime.product_settings.settings_only()["compute"]
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    save = runtime.product_settings.save_compute_model

    def delayed_save(model_id, device, **kwargs):
        if model_id != "paraformer" or device != "auto":
            return save(model_id, device, **kwargs)
        result = save(model_id, device, **kwargs) if already_written else None
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "测试未释放设置写盘"
        return result if already_written else save(model_id, device, **kwargs)

    monkeypatch.setattr(runtime.product_settings, "save_compute_model", delayed_save)
    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        task = asyncio.create_task(runtime.switch_compute_device({"model_id": "paraformer", "device": "auto"}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.to_thread(runtime.product_settings.save_compute_model, "emotion", "auto", active_device="cpu")
            runtime._closed = close
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # 重复取消也不能让未完成的写线程脱离事务。
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    persisted = json.loads(runtime.product_settings.path.read_text("utf-8"))["compute"]
    assert persisted == runtime.product_settings.settings_only()["compute"]
    assert persisted["devices"]["paraformer"] == before["devices"]["paraformer"]
    assert persisted["fallback_devices"]["paraformer"] == before["fallback_devices"]["paraformer"]
    assert persisted["devices"]["emotion"] == "auto"
    assert runtime.config.compute.devices["paraformer"] == "cpu"
    assert [entry.args[0] for entry in runtime.asr.configure_compute.await_args_list] == (["auto"] if close else ["auto", "cpu"])
    assert runtime.state.listening is (not close)


@pytest.mark.asyncio
async def test_failed_settings_restore_after_cancel_keeps_listener_stopped(tmp_path, monkeypatch):
    """取消后的旧设置若无法写回，模型可回滚但不得恢复监听或报告成功。"""
    runtime = make_runtime(tmp_path)
    runtime.asr = SimpleNamespace(configure_compute=AsyncMock(side_effect=[
        model_status("cuda", requested="auto"), model_status("cpu"),
    ]))
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    save = runtime.product_settings.save_compute_model

    def delayed_save(model_id, device, **kwargs):
        if device == "cpu":
            raise OSError("模拟旧设置恢复失败")
        result = save(model_id, device, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr(runtime.product_settings, "save_compute_model", delayed_save)
    with patch("voice_terminal.application.resource_readiness.inspect_model_resources", return_value={}):
        task = asyncio.create_task(runtime.switch_compute_device({"model_id": "paraformer", "device": "auto"}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="恢复原设置失败.*模拟旧设置恢复失败"):
            await task
    assert runtime.state.listening is False
    runtime.start_listening.assert_not_awaited()
    assert [entry.args[0] for entry in runtime.asr.configure_compute.await_args_list] == ["auto", "cpu"]
