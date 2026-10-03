"""本地模型运行设备与资源状态。"""

from __future__ import annotations

from importlib import metadata
import json
import os
import sys
import time
import threading
import copy
import subprocess
from typing import Any


def process_memory_bytes() -> int | None:
    """返回当前工作进程的常驻内存（RSS，操作系统驻留集）。"""
    try:
        import psutil  # type: ignore

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def _backend_hardware_status() -> dict[str, Any]:
    """探测当前机器的 RAM（系统内存）、显存和各后端 GPU 支持。"""
    memory: dict[str, int | None] = {"available_bytes": None, "total_bytes": None}
    try:
        import psutil  # type: ignore

        values = psutil.virtual_memory()
        memory = {"available_bytes": int(values.available), "total_bytes": int(values.total)}
    except Exception:
        pass

    torch_cuda = False
    gpu: dict[str, Any] = {
        "available": False,
        "name": None,
        "free_bytes": None,
        "total_bytes": None,
    }
    try:
        import torch  # type: ignore

        torch_cuda = bool(torch.cuda.is_available())
        if torch_cuda:
            free, total = torch.cuda.mem_get_info(0)
            gpu.update({
                "available": True,
                "name": torch.cuda.get_device_name(0),
                "free_bytes": int(free),
                "total_bytes": int(total),
            })
    except Exception:
        torch_cuda = False
    if not gpu["available"]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.free,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            )
            first = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
            parts = [part.strip() for part in first.rsplit(",", 2)]
            if len(parts) == 3:
                name, free_mib, total_mib = parts
                gpu.update({
                    "available": True,
                    "name": name,
                    "free_bytes": int(float(free_mib) * 1024 * 1024),
                    "total_bytes": int(float(total_mib) * 1024 * 1024),
                })
        except Exception:
            pass

    ort_providers: list[str] = []
    try:
        import onnxruntime as ort  # type: ignore

        ort_providers = [str(item) for item in ort.get_available_providers()]
    except Exception:
        pass

    sherpa_version: str | None = None
    try:
        sherpa_version = metadata.version("sherpa-onnx")
    except metadata.PackageNotFoundError:
        try:
            import sherpa_onnx  # type: ignore

            sherpa_version = str(getattr(sherpa_onnx, "__version__", "")) or None
        except Exception:
            pass
    sherpa_cuda = bool(sherpa_version and "cuda" in sherpa_version.lower())
    onnx_cuda = "CUDAExecutionProvider" in ort_providers

    models = {
        "paraformer": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "未检测到可用的 PyTorch CUDA 后端。"},
        "sensevoice": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "未检测到可用的 PyTorch CUDA 后端。"},
        "voice_vad": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "未检测到可用的 PyTorch CUDA 后端。"},
        "kws": {"gpu_available": sherpa_cuda, "reason": None if sherpa_cuda else "未安装 Sherpa-ONNX CUDA 版本。"},
        "smart_turn": {"gpu_available": onnx_cuda, "reason": None if onnx_cuda else "ONNX Runtime 未提供 CUDAExecutionProvider（CUDA 执行提供程序）。"},
        "wespeaker": {"gpu_available": onnx_cuda, "reason": None if onnx_cuda else "ONNX Runtime 未提供 CUDAExecutionProvider（CUDA 执行提供程序）。"},
    }
    return {
        "system_memory": memory,
        "gpu": gpu,
        "backends": {
            "pytorch_cuda": torch_cuda,
            "onnxruntime_providers": ort_providers,
            "sherpa_onnx_version": sherpa_version,
            "sherpa_onnx_cuda": sherpa_cuda,
        },
        "models": models,
        "capabilities_sampled_at": time.time(),  # capabilities_sampled_at（后端能力采样时间）。
    }


_capabilities: dict | None = None
_capability_lock = threading.Lock()


def hardware_status() -> dict[str, Any]:
    """主服务使用宿主能力快照；独立入口只运行一次隔离探测，不导入重型后端。"""
    if os.environ.get("VOICE_RESOURCE_ROLE") != "voice":
        return _backend_hardware_status()
    global _capabilities
    with _capability_lock:
        if _capabilities is None:
            serialized = os.environ.get("VOICE_HARDWARE_CAPABILITIES")
            if serialized:
                _capabilities = json.loads(serialized)
            else:
                env = {**os.environ, "VOICE_RESOURCE_ROLE": "bootstrap"}
                result = subprocess.run([sys.executable, "-m", "voice_terminal.hardware_probe"],
                                        capture_output=True, text=True, timeout=60, env=env, check=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                _capabilities = json.loads(result.stdout.splitlines()[-1])
    result = copy.deepcopy(_capabilities)
    try:
        import psutil
        memory = psutil.virtual_memory()
        result["system_memory"] = {"available_bytes": memory.available, "total_bytes": memory.total}
    except Exception:
        pass
    # 宿主定期从已经加载的工作进程刷新显存数值；主服务不初始化 CUDA 上下文。
    return result


def accept_hardware_capabilities(value: dict) -> None:
    """已加载工作进程的能力报告（校验最小结构，保留未知兼容字段）。"""
    if not isinstance(value, dict) or not all(isinstance(value.get(k), dict) for k in ("models", "gpu", "backends", "system_memory")):
        raise ValueError("硬件能力报告无效")
    global _capabilities
    with _capability_lock:
        _capabilities = copy.deepcopy(value)


def actual_device(mode: str, *, gpu_supported: bool) -> str:
    """将 auto（自动）/cpu（中央处理器）/cuda（图形处理器）解析成启动设备。"""
    if mode == "cpu":
        return "cpu"
    return "cuda" if gpu_supported else "cpu"


_GPU_MINIMUM_FREE_BYTES = {
    "paraformer": 1024 * 1024 * 1024,
    "sensevoice": 1536 * 1024 * 1024,
    "voice_vad": 384 * 1024 * 1024,
    "kws": 256 * 1024 * 1024,
    "smart_turn": 256 * 1024 * 1024,
    "wespeaker": 256 * 1024 * 1024,
}


def startup_device(mode: str, model_id: str, *, status: dict[str, Any] | None = None) -> str:
    """按当前内存能力解析启动设备；显式 CUDA 仍交给加载器尝试并回退。"""
    if mode == "cpu":
        return "cpu"
    hardware = status or hardware_status()
    gpu = hardware.get("models", {}).get(model_id, {})
    if mode == "auto":
        free = hardware.get("gpu", {}).get("free_bytes")
        enough = isinstance(free, int) and free >= _GPU_MINIMUM_FREE_BYTES.get(model_id, 0)
        return "cuda" if gpu.get("gpu_available") and enough else "cpu"
    return "cuda"


def device_candidates(mode: str, model_id: str, fallback_device: str = "cpu") -> list[str]:
    """返回加载尝试顺序：自动优先 GPU，失败时回退到最近成功设备和 CPU。"""
    initial = startup_device(mode, model_id)
    candidates = [initial]
    if initial == "cuda":
        if fallback_device in {"cpu", "cuda"} and fallback_device not in candidates:
            candidates.append(fallback_device)
        if "cpu" not in candidates:
            candidates.append("cpu")
    return candidates
