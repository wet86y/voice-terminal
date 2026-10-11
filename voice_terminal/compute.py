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
from pathlib import Path


def process_memory_bytes() -> int | None:
    """返回当前工作进程的常驻内存（RSS，操作系统驻留集）。"""
    try:
        import psutil  # type: ignore

        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def _system_commit_bytes() -> tuple[int, int] | None:
    """GetPerformanceInfo（Windows 性能接口）提供提交内存上限和剩余预算。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        class PerformanceInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                ("CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                 "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
                (name, wintypes.DWORD) for name in ("HandleCount", "ProcessCount", "ThreadCount")]
        info = PerformanceInfo()
        info.cb = ctypes.sizeof(info)
        query = ctypes.WinDLL("psapi", use_last_error=True).GetPerformanceInfo
        query.argtypes = [ctypes.POINTER(PerformanceInfo), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if query(ctypes.byref(info), info.cb):
            return int(info.CommitLimit * info.PageSize), int(max(0, info.CommitLimit - info.CommitTotal) * info.PageSize)
        return None
    except (OSError, AttributeError):
        return None


def system_commit_limit_bytes() -> int | None:
    """system_commit_limit_bytes（系统提交内存上限字节数）。"""
    values = _system_commit_bytes()
    return values[0] if values else None


def system_commit_available_bytes() -> int | None:
    """system_commit_available_bytes（系统剩余提交内存字节数），未知时不猜测。"""
    values = _system_commit_bytes()
    return values[1] if values else None


def _torch_cuda_probe(torch: Any) -> bool:
    """验证 PyTorch（张量框架）显卡计算，不能只读取编译或设备声明。"""
    try:
        if not torch.cuda.is_available():
            return False
        matrix = torch.ones((8, 8), device="cuda")
        result = (matrix @ matrix).sum().item()
        torch.cuda.synchronize()
        return result == 512.0
    except Exception:
        return False


def _onnx_cuda_probe(ort: Any) -> bool:
    """验证 ONNX Runtime（模型推理运行时）实际执行显卡节点，拒绝静默回退。"""
    try:
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return False
        import tempfile
        import base64
        import numpy as np
        ort.preload_dlls()
        with tempfile.TemporaryDirectory(prefix="voice-gpu-check-") as folder:
            # probe_model（探测模型）：两个 8×8 单精度输入执行 MatMul（矩阵乘法），
            # IR 10（模型格式版本）和 opset 17（算子版本）；不额外依赖 ONNX（模型编辑库）。
            probe_model = base64.b64decode('CAo6XQoRCgFhCgFiEgFjIgZNYXRNdWwSCWdwdS1jaGVja1oTCgFhEg4KDAgBEggKAggICgIICFoTCgFiEg4KDAgBEggKAggICgIICGITCgFjEg4KDAgBEggKAggICgIICEIECgAQEQ==')
            options = ort.SessionOptions()
            options.enable_profiling = True
            options.log_severity_level = 3
            options.intra_op_num_threads = 1
            options.profile_file_prefix = str(Path(folder) / "profile")
            session = ort.InferenceSession(probe_model, sess_options=options,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            session.disable_fallback()
            if "CUDAExecutionProvider" not in session.get_providers():
                return False
            matrix = np.ones((8, 8), dtype=np.float32)
            result = float(session.run(None, {"a": matrix, "b": matrix})[0].sum())
            trace = json.loads(Path(session.end_profiling()).read_text(encoding="utf-8"))
            return result == 512.0 and any(item.get("args", {}).get("provider") == "CUDAExecutionProvider"
                and item.get("args", {}).get("op_name") == "MatMul" for item in trace)
    except Exception:
        return False



def _backend_hardware_status(*, onnx_only: bool = False) -> dict[str, Any]:
    """探测当前机器的 RAM（系统内存）、显存和各后端 GPU 支持。"""
    memory: dict[str, int | None] = {"available_bytes": None, "total_bytes": None}
    try:
        import psutil  # type: ignore

        values = psutil.virtual_memory()
        memory = {"available_bytes": int(values.available), "total_bytes": int(values.total)}
    except Exception:
        pass

    memory["commit_limit_bytes"] = system_commit_limit_bytes()
    torch_cuda = False
    gpu: dict[str, Any] = {
        "available": False,
        "name": None,
        "free_bytes": None,
        "total_bytes": None,
    }
    if not onnx_only:
        try:
            import torch  # type: ignore

            torch_cuda = _torch_cuda_probe(torch)
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
    onnx_cuda = False
    try:
        import onnxruntime as ort  # type: ignore

        ort_providers = [str(item) for item in ort.get_available_providers()]
        onnx_cuda = _onnx_cuda_probe(ort)
    except Exception:
        pass

    sherpa_version: str | None = None
    if not onnx_only:
        try:
            sherpa_version = metadata.version("sherpa-onnx")
        except metadata.PackageNotFoundError:
            try:
                import sherpa_onnx  # type: ignore

                sherpa_version = str(getattr(sherpa_onnx, "__version__", "")) or None
            except Exception:
                pass
    sherpa_cuda = bool(gpu["available"] and sherpa_version and "cuda" in sherpa_version.lower())

    models = {
        "paraformer": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "PyTorch（张量框架）显卡后端缺失或实际计算检查未通过。"},
        "sensevoice": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "PyTorch（张量框架）显卡后端缺失或实际计算检查未通过。"},
        "voice_vad": {"gpu_available": torch_cuda, "reason": None if torch_cuda else "PyTorch（张量框架）显卡后端缺失或实际计算检查未通过。"},
        "kws": {"gpu_available": sherpa_cuda, "reason": None if sherpa_cuda else "Sherpa-ONNX（语音模型运行时）显卡后端或可用显卡缺失。"},
        "smart_turn": {"gpu_available": onnx_cuda, "reason": None if onnx_cuda else "ONNX Runtime（模型推理运行时）显卡后端缺失或实际计算检查未通过。"},
        "wespeaker": {"gpu_available": onnx_cuda, "reason": None if onnx_cuda else "ONNX Runtime（模型推理运行时）显卡后端缺失或实际计算检查未通过。"},
    }
    return {
        "system_memory": memory,
        "gpu": gpu,
        "backends": {
            "pytorch_cuda": torch_cuda,
            "onnxruntime_providers": ort_providers,
            "onnxruntime_cuda_verified": onnx_cuda,  # onnxruntime_cuda_verified（显卡实际计算检查通过）。
            "sherpa_onnx_version": sherpa_version,
            "sherpa_onnx_cuda": sherpa_cuda,
        },
        "models": models,
        "capabilities_sampled_at": time.time(),  # capabilities_sampled_at（后端能力采样时间）。
    }


_capabilities: dict | None = None
_onnx_capabilities: dict | None = None  # _onnx_capabilities（轻量推理后端的独立能力缓存）。
_capability_lock = threading.Lock()


def hardware_status(*, refresh: bool = False, onnx_only: bool = False) -> dict[str, Any]:
    """主服务使用宿主能力快照；独立入口只运行一次隔离探测，不导入重型后端。"""
    global _capabilities, _onnx_capabilities
    onnx_only = onnx_only or os.environ.get("VOICE_RESOURCE_ROLE") == "speaker"
    with _capability_lock:
        if onnx_only:
            if _onnx_capabilities is None or refresh:
                _onnx_capabilities = _backend_hardware_status(onnx_only=True)
            cached = _onnx_capabilities
        else:
            cached = _capabilities
        if not onnx_only and (_capabilities is None or refresh):
            serialized = os.environ.get("VOICE_HARDWARE_CAPABILITIES")
            if os.environ.get("VOICE_RESOURCE_ROLE") != "voice":
                _capabilities = _backend_hardware_status()
            elif serialized and not refresh:
                _capabilities = json.loads(serialized)
            else:
                env = {**os.environ, "VOICE_RESOURCE_ROLE": "bootstrap"}
                from .paths import python_command
                command = python_command(sys.executable, "voice_terminal.hardware_probe", root=next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file()))
                result = subprocess.run(command,
                                        capture_output=True, text=True, timeout=60, env=env, check=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                _capabilities = json.loads(result.stdout.splitlines()[-1])
            cached = _capabilities
    result = copy.deepcopy(cached)
    if not os.environ.get("VOICE_GPU_PROBE") and not os.environ.get("VOICE_GPU_FALLBACK"):
        from .gpu_environment import registered
        install = Path(os.environ.get("VOICE_INSTALL_ROOT", ".local"))
        data = Path(os.environ.get("VOICE_DATA_DIR", str(install / "data")))
        registration = registered(install, data)
        if registration:
            result["models"] = registration.get("models", {})
            if isinstance(registration.get("gpu_hardware"),dict):result["gpu"]=registration["gpu_hardware"]
            result["registered_environment"] = registration["path"]  # registered_environment（已验证的显卡解释器目录）。
        elif os.environ.get("VOICE_LAUNCH_CHANNEL") in {"product", "distribution"}:
            try:
                runtime_record=json.loads((install/'runtimes/python/runtime.json').read_text('utf-8'))
                compatible=runtime_record.get('profile')=='cu128'
                if (runtime_record.get('schema')==2 and runtime_record.get('profile')=='compatible-private'
                        and runtime_record.get('compatible_gpu') is True):
                    # compatible-private（已验证的兼容显卡环境）是旧 cu128（显卡运行组合）更新后的登记。
                    # 依赖锁必须仍匹配；实际后端与逐模型加载检查继续决定显卡能力。
                    import hashlib
                    program=Path(os.environ.get('VOICE_PROGRAM_ROOT',str(install/'app')))
                    lock_digest=hashlib.sha256((program/'config/runtime/voice-runtime.lock.json').read_bytes()).hexdigest()
                    compatible=runtime_record.get('lock_sha256')==lock_digest
            except (OSError,ValueError,AttributeError): compatible=False
            if not compatible or (data/'config/gpu-environment.json').is_file():
                for capability in result.get('models',{}).values():
                    capability.update(gpu_available=False,reason='显卡环境未登记或版本已变化，请在组件页面重新检测。')
    if os.environ.get("VOICE_GPU_FALLBACK"):
        for capability in result.get("models", {}).values():
            capability.update(gpu_available=False, reason="显卡环境进程启动失败，当前使用中央处理器。")
    try:
        import psutil
        memory = psutil.virtual_memory()
        result["system_memory"] = {"available_bytes": memory.available, "total_bytes": memory.total,
                                   "commit_limit_bytes": system_commit_limit_bytes()}
    except Exception:
        pass
    # 宿主定期从已经加载的工作进程刷新显存数值；主服务不初始化 CUDA 上下文。
    return result


def model_hardware_status(hardware: dict[str, Any], config: Any, *, verify: bool = False) -> dict[str, Any]:
    """结合实际模型资源校验可选设备；框架可用不代表模型显卡文件齐全。"""
    result = copy.deepcopy(hardware)
    required = {
        "paraformer": config.asr_model_dir / "model.pt",
        "sensevoice": config.model_dir / "analysis/sensevoice/model.pt",
        "voice_vad": config.vad_model_dir / "model.pt",
        "smart_turn": config.model_dir / "analysis/smart-turn/smart-turn-v3.2-gpu.onnx",
        "wespeaker": config.speaker_model_path,
    }
    for name, path in required.items():
        status = result.get("models", {}).get(name)
        if status and status.get("gpu_available") and not path.is_file():
            status.update(gpu_available=False, reason="当前模型的显卡推理资源未配置完整。")
    kws = result.get("models", {}).get("kws")
    if kws and kws.get("gpu_available") and not all(any(config.kws_dir.glob(pattern))
            for pattern in ("tokens.txt", "encoder*.onnx", "decoder*.onnx", "joiner*.onnx")):
        kws.update(gpu_available=False, reason="关键词唤醒模型的显卡推理资源未配置完整。")
    if verify and not hardware.get("registered_environment"):
        smart_turn = result.get("models", {}).get("smart_turn")
        if smart_turn and smart_turn.get("gpu_available") and not (required["smart_turn"].parent / "preprocessor_config.json").is_file():
            smart_turn.update(gpu_available=False, reason="语音结束判断模型的音频特征配置缺失。")
        # 单个矩阵运算能通过，不代表卷积、模型算子或相应动态库可以执行。
        for name in ("smart_turn", "wespeaker"):
            status = result.get("models", {}).get(name)
            if status and status.get("gpu_available"):
                verified = _isolated_onnx_model_probe(name, required[name], hardware.get("capabilities_sampled_at"))
                status.update(gpu_available=verified["ready"], reason=verified.get("reason"))
        for name in ("paraformer", "sensevoice", "voice_vad"):
            status = result.get("models", {}).get(name)
            if status and status.get("gpu_available") and not (required[name].parent / "config.yaml").is_file():
                status.update(gpu_available=False, reason="当前模型的推理配置文件缺失。")
    return result


_model_probe_cache: dict[tuple, dict] = {}  # _model_probe_cache（模型文件与能力采样对应的检查缓存）。
_model_probe_lock = threading.Lock()


def _isolated_onnx_model_probe(name: str, path: Path, capability_time: Any) -> dict:
    """在独立解释器中执行真实模型；主服务不会因环境检查占用显卡。"""
    try:
        file = path.stat()
        key = (name, str(path.resolve()), file.st_size, file.st_mtime_ns, capability_time)
        with _model_probe_lock:
            if key not in _model_probe_cache:
                from .paths import python_command
                command = python_command(sys.executable, "voice_terminal.hardware_probe", "--model", name,
                    "--model-path", str(path), root=next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file()))
                env = {**os.environ, "VOICE_RESOURCE_ROLE": "bootstrap", "PYTHONDONTWRITEBYTECODE": "1"}
                response = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45, check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                value = json.loads(response.stdout.splitlines()[-1])
                if not isinstance(value, dict) or not isinstance(value.get("ready"), bool):
                    raise ValueError("模型环境检查未返回有效结果")
                _model_probe_cache[key] = value
                if len(_model_probe_cache) > 24:
                    _model_probe_cache.clear()
                    _model_probe_cache[key] = value
            return dict(_model_probe_cache[key])
    except Exception:
        return {"ready": False, "reason": "模型的显卡推理检查未完成或失败，请刷新后重试。"}


def onnx_model_cuda_probe(name: str, path: Path) -> dict:
    """检查实际模型的显卡算子，包括所需卷积库；静默中央处理器回退无效。"""
    import tempfile
    try:
        import numpy as np
        import onnxruntime as ort
        ort.preload_dlls()
        with tempfile.TemporaryDirectory(prefix="voice-model-gpu-") as folder:
            options = ort.SessionOptions()
            options.enable_profiling = True
            options.profile_file_prefix = str(Path(folder) / "profile")
            options.log_severity_level = 3
            options.intra_op_num_threads = 1
            session = ort.InferenceSession(str(path), sess_options=options,
                providers=[("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC", "cudnn_conv_use_max_workspace": "0"}), "CPUExecutionProvider"])
            session.disable_fallback()
            if "CUDAExecutionProvider" not in session.get_providers():
                raise RuntimeError("显卡执行后端未能加载")
            inputs = session.get_inputs()
            if len(inputs) != 1 or inputs[0].type != "tensor(float)" or len(inputs[0].shape) != 3:
                raise ValueError("模型输入格式未通过检查")
            # SmartTurn（结束判断）使用八秒特征；WeSpeaker（声纹识别）使用完整特征帧。
            defaults = (1, 80, 800) if name == "smart_turn" else (1, 160, 80)
            shape = [axis if isinstance(axis, int) and axis > 0 else defaults[index] for index, axis in enumerate(inputs[0].shape)]
            result = session.run(None, {inputs[0].name: np.zeros(shape, dtype=np.float32)})
            trace = json.loads(Path(session.end_profiling()).read_text(encoding="utf-8"))
            executed = any(row.get("args", {}).get("provider") == "CUDAExecutionProvider"
                and row.get("args", {}).get("op_name") in {"Conv", "FusedConv", "MatMul", "Gemm", "Attention"} for row in trace)
            if not executed or not result or not all(np.isfinite(value).all() for value in result):
                raise RuntimeError("模型没有完成有效的显卡推理")
            return {"ready": True, "reason": None}
    except Exception as exc:
        # 不输出文件位置或运行环境内容，只返回可用于设置页的简短诊断。
        return {"ready": False, "reason": f"实际模型的显卡推理检查失败（{type(exc).__name__}），请检查模型算子与显卡运行库。"}


def accept_hardware_capabilities(value: dict) -> None:
    """已加载工作进程的能力报告（校验最小结构，保留未知兼容字段）。"""
    if not isinstance(value, dict) or not all(isinstance(value.get(k), dict) for k in ("models", "gpu", "backends", "system_memory")):
        raise ValueError("硬件能力报告无效")
    global _capabilities
    with _capability_lock:
        _capabilities = copy.deepcopy(value)


def actual_device(mode: str, *, gpu_supported: bool) -> str:
    """将 auto（自动）/cpu（中央处理器）/cuda（图形处理器）解析成启动设备。"""
    if mode == "cpu" or os.environ.get("VOICE_GPU_FALLBACK"):
        return "cpu"
    return "cuda" if gpu_supported else "cpu"


def loaded_funasr_device(model: Any, attempted: str) -> str:
    """读取 FunASR（语音模型加载器）解析后的设备，包含其内部静默回退。"""
    options = getattr(model, "kwargs", None)
    value = options.get("device") if isinstance(options, dict) else None
    if isinstance(value, str):
        backend = value.split(":", 1)[0].lower()
        if backend in {"cpu", "cuda"}:
            return backend
    # 兼容未提供 kwargs（实际加载参数）的旧加载器及测试替身。
    return attempted


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
    if mode == "cpu" or os.environ.get("VOICE_GPU_FALLBACK"):
        return "cpu"
    if mode == "cuda":
        # 显式设备交给实际模型加载器验证并回退，无需先加载无关框架。
        return "cuda"
    hardware = status or hardware_status()
    gpu = hardware.get("models", {}).get(model_id, {})
    if mode == "auto":
        free = hardware.get("gpu", {}).get("free_bytes")
        enough = isinstance(free, int) and free >= _GPU_MINIMUM_FREE_BYTES.get(model_id, 0)
        return "cuda" if gpu.get("gpu_available") and enough else "cpu"
    return "cuda"


def device_candidates(mode: str, model_id: str, fallback_device: str = "cpu", *,
                      onnx_only: bool = False) -> list[str]:
    """返回加载尝试顺序：自动优先 GPU，失败时回退到最近成功设备和 CPU。"""
    status = hardware_status(onnx_only=True) if onnx_only and mode == "auto" and not os.environ.get("VOICE_GPU_FALLBACK") else None
    initial = startup_device(mode, model_id, status=status)
    candidates = [initial]
    if initial == "cuda":
        if fallback_device in {"cpu", "cuda"} and fallback_device not in candidates:
            candidates.append(fallback_device)
        if "cpu" not in candidates:
            candidates.append("cpu")
    return candidates
