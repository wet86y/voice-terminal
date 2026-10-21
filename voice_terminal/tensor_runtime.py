"""Tensor runtime（张量依赖选择）：同一解释器按进程选择处理器或显卡包。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def cuda_tensor_loaded() -> bool:
    """tensor_cuda_build（显卡张量构建已加载）：仅查询已有模块，不导入后端。"""
    loaded = sys.modules.get("torch")
    return bool(getattr(getattr(loaded, "version", None), "cuda", None))


def activate_tensor_runtime(device: str | None = None, *, runtime: Path | None = None):
    """在首次导入前选择固定包目录，已加载的张量后端只能通过进程重启切换。"""
    selected = device or os.environ.get("VOICE_TENSOR_DEVICE", "cpu")
    if os.environ.get("VOICE_GPU_FALLBACK"):
        selected = "cpu"
    root = runtime or Path(sys.executable).parent
    overlay = root / "Lib/cpu-packages"
    try:
        record = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    shared_gpu = bool(record.get("gpu_profile") or record.get("compatible_gpu") or record.get("profile") == "cu128")
    required = ["torch", "torchaudio"]
    if any((root / "Lib/site-packages").glob("onnxruntime_gpu-*.dist-info")):
        required.append("onnxruntime")
    overlay_ready = all((overlay / name / "__init__.py").is_file() for name in required)
    if selected == "cpu" and shared_gpu and not overlay_ready:
        raise RuntimeError("显卡环境缺少独立 CPU（中央处理器）张量或推理包，请更新语音运行依赖。")
    loaded = sys.modules.get("torch")
    if loaded is not None:
        cuda_build = bool(getattr(getattr(loaded, "version", None), "cuda", None))
        if selected == "cpu" and cuda_build and shared_gpu:
            raise RuntimeError("处理器进程已加载显卡张量库，请重新启动应用以应用独立依赖。")
        if selected in {"cuda", "auto"} and not cuda_build and shared_gpu:
            raise RuntimeError("进程已加载处理器张量库，请重新启动应用以应用显卡依赖。")
        return
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != overlay.resolve()]
    if selected == "cpu":
        if overlay_ready:
            sys.path.insert(0, str(overlay))
    elif selected not in {"cuda", "auto"}:
        raise ValueError("未知的张量依赖设备")
