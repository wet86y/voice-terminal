"""识别父、子进程依赖分离，避免处理器标点常驻显卡张量库。"""
from pathlib import Path
from dataclasses import replace
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.application.supervisor import ApplicationHost
from voice_terminal.config import AppConfig, AsrConfig, ComputeConfig, COMPUTE_MODEL_IDS


@pytest.mark.asyncio
@pytest.mark.parametrize("punctuation_device", [None, "cpu", "cuda", "auto"])
async def test_asr_parent_environment_follows_punctuation_instead_of_main_model(
        tmp_path, monkeypatch, punctuation_device):
    host = ApplicationHost(Path.cwd())
    host.paths = SimpleNamespace(install_root=tmp_path, data_root=tmp_path / "data",
        config=tmp_path / "config", logs=tmp_path / "logs", environment=lambda: {})
    host.python = "cpu-python"
    host.manifest = {"compute_devices": {"paraformer": "cuda"}, "startup_gpu_roles": ["asr"]}
    if punctuation_device is not None:
        host.manifest["asr_parent_device"] = punctuation_device
    selected = punctuation_device or "cpu"
    captured = {}
    expected_python = "gpu-python" if selected != "cpu" else "cpu-python"

    def choose(_install, _data, role, cpu_python, devices):
        assert role == "asr" and cpu_python == "cpu-python"
        assert devices["paraformer"] == selected
        return expected_python

    def create(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        host.state.value["endpoints"]["asr"] = {
            "url": "ws://127.0.0.1:1/asr", "generation": host.generations["asr"]}
        return SimpleNamespace(pid=123, created_at=100, poll=lambda: None)

    monkeypatch.setattr("voice_terminal.gpu_environment.worker_python", choose)
    monkeypatch.setattr("voice_terminal.paths.python_command", lambda executable, module, *args, **kwargs: [executable, module])
    monkeypatch.setattr("voice_terminal.application.supervisor.ManagedProcess", create)
    host.probe = AsyncMock(return_value=True)
    try:
        await host.start_service("asr")
        assert captured["command"][0] == expected_python
        assert captured["env"]["VOICE_TENSOR_DEVICE"] == selected
        assert captured["env"]["VOICE_CPU_PYTHON"] == "cpu-python"
        assert host.manifest["compute_devices"]["paraformer"] == "cuda"
    finally:
        host.processes.clear()  # 所有进程均为替身，没有系统进程需要回收。
        host.outer_job.close()


@pytest.mark.parametrize("enabled,device,expected", [(True, "cpu", "cpu"), (True, "cuda", "cuda"), (False, "cuda", "cpu")])
def test_bootstrap_reports_parent_device_and_budgets_explicit_gpu_punctuation(
        tmp_path, monkeypatch, capsys, enabled, device, expected):
    """启动清单按实际标点用途选父进程依赖，显卡标点也要进入加载预算。"""
    from voice_terminal.application import bootstrap
    config = replace(AppConfig(model_dir=tmp_path / "models"),
        asr=AsrConfig(device="cpu", punctuation_enabled=enabled, punctuation_device=device),
        compute=ComputeConfig(devices={name: "cpu" for name in COMPUTE_MODEL_IDS}))
    paths = SimpleNamespace(env_file=tmp_path / ".env", config=tmp_path / "config", install_root=tmp_path, separated=False)
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--config", str(tmp_path / "local.yaml")])
    monkeypatch.setattr(bootstrap.AppPaths, "resolve", lambda *_: paths)
    monkeypatch.setattr("voice_terminal.config.load_config", lambda *_: config)
    monkeypatch.setattr("voice_terminal.server.product.ProductSettingsStore",
                        lambda **_: SimpleNamespace(effective_config=lambda: config))
    monkeypatch.setattr(bootstrap, "inspect_startup_resources", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(bootstrap, "read_portable", lambda _: {"dsh": "runtimes/dsh"})
    monkeypatch.setattr("voice_terminal.dependencies.diagnose", lambda **_: {})
    bootstrap.main()
    manifest = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert manifest["asr_parent_device"] == expected
    assert ("asr" in manifest["startup_gpu_roles"]) == (expected != "cpu")
