"""CPU（处理器）与 CUDA（显卡）依赖目录的进程级隔离。"""
import json
import sys
from types import SimpleNamespace

import pytest

from voice_terminal.tensor_runtime import activate_tensor_runtime


def runtime(tmp_path):
    (tmp_path / "runtime.json").write_text(json.dumps({"gpu_profile": "cu128"}), encoding="utf-8")
    overlay = tmp_path / "Lib/cpu-packages"
    for name in ("torch", "torchaudio"):
        package = overlay / name
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
    return overlay


def test_cpu_and_gpu_processes_choose_separate_packages_without_switching_interpreter(tmp_path, monkeypatch):
    overlay = runtime(tmp_path)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "path", ["unchanged"])
    activate_tensor_runtime("cpu", runtime=tmp_path)
    assert sys.path == [str(overlay), "unchanged"]
    activate_tensor_runtime("cuda", runtime=tmp_path)
    assert sys.path == ["unchanged"]


def test_cpu_does_not_silently_use_gpu_packages_if_overlay_missing(tmp_path, monkeypatch):
    (tmp_path / "runtime.json").write_text('{"profile":"cu128"}', encoding="utf-8")
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(RuntimeError, match="缺少独立 CPU"):
        activate_tensor_runtime("cpu", runtime=tmp_path)


def test_cpu_fallback_overrides_gpu_selection_before_import(tmp_path, monkeypatch):
    overlay = runtime(tmp_path)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "path", ["unchanged"])
    monkeypatch.setenv("VOICE_GPU_FALLBACK", "1")
    activate_tensor_runtime("cuda", runtime=tmp_path)
    assert sys.path[0] == str(overlay)


def test_loaded_gpu_backend_requires_new_process_for_cpu_mode(tmp_path, monkeypatch):
    runtime(tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda="12.8")))
    with pytest.raises(RuntimeError, match="重新启动"):
        activate_tensor_runtime("cpu", runtime=tmp_path)


def test_legacy_gpu_onnx_requires_cpu_inference_package_too(tmp_path, monkeypatch):
    overlay = runtime(tmp_path)
    (tmp_path / "Lib/site-packages/onnxruntime_gpu-1.24.4.dist-info").mkdir(parents=True)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "path", ["unchanged"])
    with pytest.raises(RuntimeError, match="推理包"):
        activate_tensor_runtime("cpu", runtime=tmp_path)
    (overlay / "onnxruntime").mkdir()
    (overlay / "onnxruntime/__init__.py").touch()
    activate_tensor_runtime("cpu", runtime=tmp_path)
    assert sys.path[0] == str(overlay)


def test_loaded_cpu_backend_requires_new_process_for_gpu_mode(tmp_path, monkeypatch):
    runtime(tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda=None)))
    with pytest.raises(RuntimeError, match="重新启动"):
        activate_tensor_runtime("cuda", runtime=tmp_path)
