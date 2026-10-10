"""单侧运行时资源测量器；由 benchmark_model_memory.py 顺序启动。

所有英文指标名均在 JSON 的 field_descriptions_zh 中给出中文解释。此脚本
不访问麦克风或云服务，也不会自动下载或导出模型。
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from collections import defaultdict, deque
import gc
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
import traceback
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from vad_validation_support import build_vad_detector


def _json_value(value: Any) -> Any:
    """将模型结果转为可写 JSON 的普通值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except Exception:
            pass
    if hasattr(value, "tolist"):
        try:
            return _json_value(value.tolist())
        except Exception:
            pass
    return repr(value)


def _digest(value: Any) -> str:
    canonical = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(values)
    return {"p50": statistics.median(ordered),
            "p95": ordered[min(len(ordered) - 1, math.ceil(len(ordered) * .95) - 1)],
            "p99": ordered[min(len(ordered) - 1, math.ceil(len(ordered) * .99) - 1)]}


class WindowsGpuCounters:
    """按进程枚举 Windows PDH GPU Process Memory 计数器。

    Dedicated Usage（专用显存用量）与 Shared Usage（共享显存用量）分别采样。
    计数器/权限不可用时返回 None；已成功枚举但本进程无条目时才记为零。
    """
    PATHS = {
        "gpu_dedicated_bytes": r"\GPU Process Memory(*)\Dedicated Usage",
        "gpu_shared_bytes": r"\GPU Process Memory(*)\Shared Usage",
    }

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self._pdh = None
        self._query = wintypes.HANDLE()
        self._counters: dict[str, wintypes.HANDLE] = {}
        self._init_error: str | None = None
        if not self.available:
            self._init_error = "当前不是 Windows，系统 PDH GPU 进程计数器不可用"
            return
        try:
            self._pdh = ctypes.WinDLL("pdh")
            pdh = self._pdh
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t,
                                          ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhOpenQueryW.restype = wintypes.LONG
            pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                                  ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
            pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            pdh.PdhCollectQueryData.restype = wintypes.LONG
            pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            pdh.PdhGetFormattedCounterArrayW.restype = wintypes.LONG
            pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
            pdh.PdhCloseQuery.restype = wintypes.LONG
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                raise OSError("PdhOpenQueryW 调用失败")
            for key, path in self.PATHS.items():
                handle = wintypes.HANDLE()
                status = pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle))
                if status == 0:
                    self._counters[key] = handle
                else:
                    self._counters[key] = None  # type: ignore[assignment]
            self.available = any(self._counters.values())
            if not self.available:
                self._init_error = "PDH 未提供 GPU Process Memory 计数器"
        except Exception as exc:
            self.available = False
            self._init_error = f"PDH 初始化失败：{type(exc).__name__}: {exc}"

    def _read_counter(self, handle: Any) -> dict[int, int | None] | None:
        if handle is None or self._pdh is None:
            return None

        class Values(ctypes.Union):
            _fields_ = [("largeValue", ctypes.c_longlong), ("doubleValue", ctypes.c_double),
                        ("longValue", wintypes.LONG)]

        class Value(ctypes.Structure):
            _anonymous_ = ("values",)
            _fields_ = [("CStatus", wintypes.DWORD), ("values", Values)]

        class Item(ctypes.Structure):
            _anonymous_ = ("FmtValue",)
            _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", Value)]

        pdh = self._pdh
        size, count = wintypes.DWORD(), wintypes.DWORD()
        status = pdh.PdhGetFormattedCounterArrayW(handle, 0x400, ctypes.byref(size),
                                                  ctypes.byref(count), None)
        # PDH_MORE_DATA: 第一次调用只取得需要的缓冲区大小。
        if (status & 0xffffffff) != 0x800007D2 or not 0 < size.value <= 16 * 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(handle, 0x400, ctypes.byref(size),
                                            ctypes.byref(count), buffer) != 0:
            return None
        if count.value > size.value // ctypes.sizeof(Item):
            return None
        result: dict[int, int | None] = {}
        items = ctypes.cast(buffer, ctypes.POINTER(Item))
        for index in range(count.value):
            match = re.match(r"pid_(\d+)_", items[index].szName or "")
            if not match:
                continue
            pid = int(match.group(1))
            value = items[index].largeValue
            if items[index].CStatus not in (0, 1) or value < 0:
                result[pid] = None
            elif pid not in result or result[pid] is not None:
                result[pid] = (result.get(pid) or 0) + int(value)
        return result

    def sample(self, pid: int) -> dict[str, int | None]:
        if not self.available:
            return {name: None for name in self.PATHS}
        # 一次 PDH 查询同时刷新两项进程级 GPU 用量。
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return {name: None for name in self.PATHS}
        output: dict[str, int | None] = {}
        for name, handle in self._counters.items():
            values = self._read_counter(handle)
            output[name] = values.get(pid, 0) if values is not None else None
        return output

    def close(self) -> None:
        if self._pdh is not None and self._query:
            self._pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()


class Sampler:
    """后台低频采集进程、系统与框架内存；阶段边界另做同步采样。"""
    def __init__(self, interval: float):
        import psutil
        self.psutil = psutil
        self.process = psutil.Process()
        self.interval = interval
        self.pid = os.getpid()
        self.rows: list[dict[str, Any]] = []
        self.phase_name = "harness_ready"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gpu_lock = threading.Lock()
        self._phase_started_at: dict[str, float] = {"harness_ready": time.perf_counter()}
        self._phase_elapsed: dict[str, float] = defaultdict(float)
        self.gpu = WindowsGpuCounters()
        self._phase_torch_device = "cuda:0"
        self.coverage: dict[str, Any] = {
            "rss_bytes": "psutil.Process.memory_info.rss；RSS（常驻工作集），按进程计，含共享映射页，不等于独占 RAM（系统内存）。",
            "peak_wset_bytes": "Windows PROCESS_MEMORY_COUNTERS_EX.PeakWorkingSetSize（进程历史峰值工作集）；非 Windows 或接口不提供时为 null，独立于采样最大值。",
            "private_commit_bytes": "psutil.Process.memory_full_info.private；Windows 私有提交量近似值，非整个任务管理器的已提交量。获取失败为 null。",
            "system_available_physical_bytes": "psutil.virtual_memory().available；系统级可用物理内存，不是本进程专属值。",
            "gpu_dedicated_bytes": "Windows PDH（性能计数器接口）GPU Process Memory\\Dedicated Usage（进程专用 GPU 内存）。",
            "gpu_shared_bytes": "Windows PDH GPU Process Memory\\Shared Usage（进程共享 GPU 内存）。",
            "torch_allocated_bytes": "仅当 PyTorch（深度学习框架）已导入且 CUDA 已初始化时读取所选设备的当前张量分配量；否则 null。",
            "torch_reserved_bytes": "仅当 PyTorch（深度学习框架）已导入且 CUDA 已初始化时读取缓存分配器保留量；否则 null。",
            "torch_num_threads": "PyTorch（深度学习框架）实际 CPU（中央处理器）计算线程数，按采样阶段记录。",
            "torch_num_interop_threads": "PyTorch（深度学习框架）实际算子间线程数，按采样阶段记录。",
        }
        self.coverage_status: dict[str, str] = {
            "gpu_dedicated_bytes": "available" if self.gpu.available and self.gpu._counters.get("gpu_dedicated_bytes") else "unavailable",
            "gpu_shared_bytes": "available" if self.gpu.available and self.gpu._counters.get("gpu_shared_bytes") else "unavailable",
            "torch_allocated_bytes": "not_imported",
            "torch_reserved_bytes": "not_imported",
        }

    def _torch_memory(self) -> tuple[int | None, int | None]:
        torch = sys.modules.get("torch")
        if torch is None:
            self.coverage_status["torch_allocated_bytes"] = "not_imported"
            self.coverage_status["torch_reserved_bytes"] = "not_imported"
            return None, None
        try:
            if not torch.cuda.is_available() or not torch.cuda.is_initialized():
                self.coverage_status["torch_allocated_bytes"] = "cuda_not_initialized"
                self.coverage_status["torch_reserved_bytes"] = "cuda_not_initialized"
                return None, None
            device = self._phase_torch_device
            allocated = int(torch.cuda.memory_allocated(device))
            reserved = int(torch.cuda.memory_reserved(device))
            self.coverage_status["torch_allocated_bytes"] = "available"
            self.coverage_status["torch_reserved_bytes"] = "available"
            return allocated, reserved
        except Exception as exc:
            status = f"unavailable:{type(exc).__name__}"
            self.coverage_status["torch_allocated_bytes"] = status
            self.coverage_status["torch_reserved_bytes"] = status
            return None, None

    def snapshot(self, *, boundary: bool = False) -> None:
        try:
            memory = self.process.memory_info()
            rss = int(memory.rss)
            peak_wset = getattr(memory, "peak_wset", None)
            peak_wset = int(peak_wset) if peak_wset is not None else None
        except Exception:
            rss = None
            peak_wset = None
        try:
            full = self.process.memory_full_info()
            private = getattr(full, "private", None)
            private = int(private) if private is not None else None
        except Exception:
            private = None
        try:
            available = int(self.psutil.virtual_memory().available)
        except Exception:
            available = None
        try:
            threads = int(self.process.num_threads())
        except Exception:
            threads = None
        with self._gpu_lock:
            gpu = self.gpu.sample(self.pid)
        torch_allocated, torch_reserved = self._torch_memory()
        torch_num_threads = torch_num_interop_threads = None
        torch = sys.modules.get("torch")
        if torch is not None:
            try:
                torch_num_threads = int(torch.get_num_threads())
                torch_num_interop_threads = int(torch.get_num_interop_threads())
            except Exception:
                pass
        now_mono = time.perf_counter()
        row = {
            "elapsed_seconds": now_mono - getattr(self, "started_at", now_mono),
            "phase": self.phase_name,
            "boundary_sample": boundary,
            "rss_bytes": rss,
            "peak_wset_bytes": peak_wset,
            "private_commit_bytes": private,
            "system_available_physical_bytes": available,
            "gpu_dedicated_bytes": gpu["gpu_dedicated_bytes"],
            "gpu_shared_bytes": gpu["gpu_shared_bytes"],
            "torch_allocated_bytes": torch_allocated,
            "torch_reserved_bytes": torch_reserved,
            "thread_count_includes_sampler_thread": self._thread is not None and self._thread.is_alive(),
            "thread_count": threads,
            "torch_num_threads": torch_num_threads,
            "torch_num_interop_threads": torch_num_interop_threads,
        }
        with self._lock:
            self.rows.append(row)

    def start(self) -> None:
        self.started_at = time.perf_counter()
        self.snapshot(boundary=True)
        self._thread = threading.Thread(target=self._sample_loop, name="resource-sampler", daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.snapshot()

    def phase(self, phase: str, *, torch_device: str | None = None) -> None:
        now = time.perf_counter()
        with self._lock:
            previous = self.phase_name
            if phase != previous:
                self._phase_elapsed[previous] += now - self._phase_started_at.get(previous, now)
                self._phase_started_at[phase] = now
            self.phase_name = phase
            if torch_device:
                self._phase_torch_device = torch_device
        self.snapshot(boundary=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval * 4))
        self.snapshot(boundary=True)
        now = time.perf_counter()
        with self._lock:
            self._phase_elapsed[self.phase_name] += now - self._phase_started_at.get(self.phase_name, now)
        self.gpu.close()

    def report(self) -> dict[str, Any]:
        with self._lock:
            rows = list(self.rows)
        by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_phase[row["phase"]].append(row)
        peaks: dict[str, Any] = {}
        metrics = ("rss_bytes", "peak_wset_bytes", "private_commit_bytes", "gpu_dedicated_bytes", "gpu_shared_bytes",
                   "torch_allocated_bytes", "torch_reserved_bytes")
        for phase, phase_rows in by_phase.items():
            peaks[phase] = {metric: max((row[metric] for row in phase_rows if row[metric] is not None), default=None)
                            for metric in metrics}
            peaks[phase]["sample_count"] = len(phase_rows)
        overall = {metric: max((row[metric] for row in rows if row[metric] is not None), default=None)
                   for metric in metrics}
        overall["system_available_physical_bytes_min"] = min(
            (row["system_available_physical_bytes"] for row in rows
             if row["system_available_physical_bytes"] is not None), default=None)
        torch_threads_by_phase: dict[str, dict[str, list[int]]] = {}
        for phase, phase_rows in by_phase.items():
            torch_threads_by_phase[phase] = {}
            for key in ("torch_num_threads", "torch_num_interop_threads"):
                torch_threads_by_phase[phase][key] = sorted(
                    {row[key] for row in phase_rows if row[key] is not None})
        return {"samples": rows, "phase_peaks": peaks, "sampled_peaks": overall,
                "coverage": self.coverage, "coverage_status": dict(self.coverage_status),
                "torch_thread_counts_by_phase": torch_threads_by_phase,
                "stage_durations_ms": {key: value * 1000 for key, value in self._phase_elapsed.items()},
                "gpu_counter_error": self.gpu._init_error,
                "sampling_interval_seconds": self.interval,
                "sampling_is_periodic_peak_not_os_hardware_peak": True}


def _actual_device(role: str, model: Any, backend: str) -> dict[str, Any]:
    device = getattr(model, "device", None)
    if role == "refine" and backend == "onnx":
        device = "cpu"
    if role == "speaker" and getattr(model, "session", None) is not None:
        providers = model.session.get_providers()
        device = "cuda" if "CUDAExecutionProvider" in providers else "cpu"
    if role == "endpoint":
        providers = getattr(model, "providers", None)
        if providers is None and getattr(model, "session", None) is not None:
            providers = model.session.get_providers()
        device = "cuda" if providers and "CUDAExecutionProvider" in providers else "cpu"
    extra: dict[str, Any] = {"device": device or "unknown"}
    funasr_modules = sorted(name for name in sys.modules
                            if name == "funasr" or name.startswith("funasr."))
    funasr_onnx_modules = sorted(name for name in sys.modules
                                 if name == "funasr_onnx" or name.startswith("funasr_onnx."))
    extra["funasr_module_count"] = len(funasr_modules)
    extra["funasr_module_names"] = funasr_modules
    extra["funasr_onnx_module_count"] = len(funasr_onnx_modules)
    extra["funasr_onnx_module_names"] = funasr_onnx_modules
    extra["asr_import_mode_requested"] = os.environ.get("VOICE_ASR_IMPORT_MODE")
    extra["weight_load_mode_requested"] = os.environ.get("VOICE_MODEL_LOAD_MODE")
    load_module = sys.modules.get("funasr.train_utils.load_pretrained_model")
    load_function = getattr(load_module, "load_pretrained_model", None) if load_module is not None else None
    extra["weight_load_mode_applied"] = getattr(load_function, "__voice_model_load_mode__", "original") \
        if load_module is not None else None
    for name in ("load_error", "backend", "runtime", "internal_vad_device", "providers", "model_file"):
        value = getattr(model, name, None)
        if value is not None:
            extra[name] = _json_value(value)
    if role == "refine" and backend == "onnx":
        extra["runtime"] = "funasr_onnx ONNX Runtime CPU（FunASR ONNX 推理运行时中央处理器）"
        extra["model_file"] = "model_quant.onnx"
    if role == "speaker" and getattr(model, "session", None) is not None:
        extra["providers"] = _json_value(model.session.get_providers())
    if role == "endpoint" and getattr(model, "session", None) is not None:
        extra["providers"] = _json_value(model.session.get_providers())
    return extra


def _semantic_output(value: Any) -> Any:
    """抽取语音精修文本与标签，便于 ONNX/FunASR 输出做语义对照。"""
    if isinstance(value, str):
        tags = re.findall(r"<\|([^|]+)\|>", value)
        known_languages = {"zh", "en", "yue", "ja", "ko", "nospeech"}
        known_emotions = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}
        known_events = {"Speech", "BGM", "Applause", "Laughter", "Cry", "Sneeze", "Breath", "Cough"}
        return {"text": re.sub(r"<\|[^|]+\|>", "", value).strip(),
                "language": next((tag for tag in tags if tag in known_languages), "unknown"),
                "emotion": next((tag for tag in tags if tag in known_emotions), "unknown"),
                "events": [tag for tag in tags if tag in known_events], "raw_tags": tags}
    if isinstance(value, dict):
        wanted = ("text", "language", "emotion", "events", "raw_tags", "no_speech", "complete")
        output = {key: _json_value(value[key]) for key in wanted if key in value}
        if "segments" in value:
            output["segments"] = [_semantic_output(item) for item in value["segments"]]
        return output
    if isinstance(value, (list, tuple)):
        return [_semantic_output(item) for item in value]
    return _json_value(value)


def _semantic_signature(value: Any, role: str) -> Any:
    """精修角色按全文聚合标签，使两后端的分段差异不遮蔽语义字段。"""
    normalized = _semantic_output(value)
    if role != "refine":
        return normalized
    if isinstance(normalized, dict):
        segments = normalized.get("segments")
        parts = segments if isinstance(segments, list) and segments else [normalized]
        text = normalized.get("text", "")
    elif isinstance(normalized, list):
        parts = normalized
        text = "".join(item.get("text", "") for item in parts if isinstance(item, dict))
    else:
        parts = [normalized]
        text = str(normalized)

    def unique_values(name: str) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            value = part.get(name)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and item not in seen:
                    seen.add(item)
                    output.append(item)
        return output

    return {"text": text, "language_tags": unique_values("language"),
            "emotion_tags": unique_values("emotion"), "event_tags": unique_values("events")}


def _build_model(args: argparse.Namespace, np: Any, pcm: bytes, sampler: Sampler) -> tuple[Any, Any]:
    """导入并加载一个角色，返回 (模型对象, 推理函数)。"""
    role = args.role
    if role == "vad":
        sampler.phase("model_import")
        from voice_terminal.resources import ResourcePolicy
        from voice_terminal.vad.detector import FsmnVadDetector
        sampler.phase("model_load")
        vad_dir = args.vad_dir or args.model_root / "funasr/fsmn-vad"
        if args.backend == "onnx" and args.device != "cpu":
            raise ValueError("ONNX VAD（轻量语音活动检测）基准只支持 --device cpu")
        detector = build_vad_detector(
            model_dir=vad_dir,
            expected_backend=args.backend,
            device=args.device,
            end_silence_ms=200,
            native_threads={"voice": args.threads} if args.threads is not None else None,
            detector_factory=FsmnVadDetector,
            resource_policy_class=ResourcePolicy,
        )
        if detector.backend != args.backend:
            raise AssertionError(
                f"requested backend（要求后端）={args.backend!r}, "
                f"actual backend（实际后端）={detector.backend!r}"
            )
        sampler.phase("explicit_warmup", torch_device=str(getattr(detector, "device", "cuda:0")))

        def run(data: bytes) -> Any:
            detector.reset()
            events = []
            for offset in range(0, len(data), 640):
                event = detector.feed(data[offset:offset + 640], is_final=offset + 640 >= len(data))
                if event.speech_started or event.speech_ended:
                    events.append({**event.__dict__, "feed_offset": offset})
            tail_event = detector.feed(b"", is_final=True)
            if tail_event.speech_started or tail_event.speech_ended:
                events.append({**tail_event.__dict__, "feed_offset": len(data), "empty_final_flush": True})
            return events
        return detector, run

    if role == "speaker":
        sampler.phase("model_import")
        from voice_terminal.speaker.engine import WeSpeakerOnnxEngine
        path = args.model_root / "speaker/wespeaker-cnceleb-resnet34/cnceleb_resnet34.onnx"
        sampler.phase("model_load")
        model = WeSpeakerOnnxEngine(path, device=args.device)
        model.load()
        sampler.phase("explicit_warmup")
        return model, lambda data: list(model.embed_pcm(data[:max(64000, 16000 * 2)]))

    if role == "asr":
        sampler.phase("model_import")
        from voice_terminal.asr.worker import AsrWorkerEngine
        sampler.phase("model_load")
        model = AsrWorkerEngine(model_dir=args.model_root / "funasr/paraformer-zh-streaming",
                                device=args.device)
        model.load()
        sampler.phase("explicit_warmup", torch_device=str(model.device))
        counter = 0
        async def infer(data: bytes) -> str:
            nonlocal counter
            counter += 1
            turn = f"benchmark-{counter}"
            result = None
            for offset in range(0, len(data), 640):
                result = await model.infer(turn, data[offset:offset + 640], offset + 640 >= len(data))
            return result.text if result is not None else ""
        return model, lambda data: asyncio.run(infer(data))

    if role == "refine" and args.backend == "onnx":
        sampler.phase("model_import")
        # 检查所有本地文件存在后才导入适配器，避免上游缺件触发在线解析或导出。
        model_dir = args.refine_model_dir
        if model_dir is None:
            raise ValueError("--backend onnx 需要显式给出 --refine-model-dir")
        required = ("model_quant.onnx", "chn_jpn_yue_eng_ko_spectok.bpe.model", "config.yaml", "am.mvn")
        missing = [name for name in required if not (model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"ONNX 精修模型缺少本地文件 {missing}；已禁止自动下载/导出")
        from funasr_onnx import SenseVoiceSmall
        import funasr_onnx.sensevoice_bin as sensevoice_module
        import sentencepiece
        # SentencePiece（子词分词器）的原生 Windows 文件路径接口不支持部分中文路径。
        # 与正式精修组件一致，用 Python 读取原文件字节；适配仅作用于本次构造。
        tokenizer_class = sensevoice_module.SentencepiecesTokenizer
        class UnicodeSentencepiece(tokenizer_class):
            def _build_sentence_piece_processor(self):
                if self.sp is None:
                    self.sp = sentencepiece.SentencePieceProcessor(model_proto=Path(self.bpemodel).read_bytes())
        sampler.phase("model_load")
        sensevoice_module.SentencepiecesTokenizer = UnicodeSentencepiece
        try:
            model = SenseVoiceSmall(model_dir=str(model_dir), quantize=True,
                                    intra_op_num_threads=args.threads or 2)
        finally:
            sensevoice_module.SentencepiecesTokenizer = tokenizer_class
        sampler.phase("explicit_warmup")
        def run(data: bytes) -> Any:
            waveform = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            return model(waveform, language="auto", textnorm="withitn")
        return model, run

    if role == "refine":
        sampler.phase("model_import")
        from voice_terminal.analysis.sensevoice import SenseVoice
        sampler.phase("model_load")
        model_dir = args.refine_model_dir or args.model_root / "analysis/sensevoice"
        vad_dir = args.vad_dir or args.model_root / "funasr/fsmn-vad"
        model = SenseVoice(model_dir, vad_dir, device=args.device)
        sampler.phase("explicit_warmup", torch_device=str(getattr(model, "device", "cuda:0")))
        return model, model

    if role == "endpoint":
        sampler.phase("model_import")
        from voice_terminal.analysis.smart_turn import SmartTurn
        sampler.phase("model_load")
        model = SmartTurn(args.model_root / "analysis/smart-turn", device=args.device)
        sampler.phase("explicit_warmup", torch_device="cuda:0")
        return model, model

    raise ValueError(f"不支持的角色：{role}")


def _release_model(role: str, model: Any, *, empty_cuda_cache: bool) -> dict[str, Any]:
    """释放 Python 持有的运行时会话/模型引用；不做设备重置。"""
    result: dict[str, Any] = {"python_references_cleared": False, "cuda_empty_cache_called": False}
    try:
        if role == "asr":
            model.close()
            model._model = None
            model._cache_by_turn.clear(); model._transcript_by_turn.clear()
            model._pending_audio_by_turn.clear()
        elif role == "vad":
            model._model = None
            model._cache.clear()
        elif role == "speaker":
            model.session = None
        elif role == "refine":
            if hasattr(model, "model"):
                model.model = None
            if hasattr(model, "vad"):
                model.vad = None
            if hasattr(model, "ort_infer"):
                model.ort_infer = None
            if hasattr(model, "session"):
                model.session = None
            if hasattr(model, "extractor"):
                model.extractor = None
        elif role == "endpoint":
            model.session = None
            model.extractor = None
        result["python_references_cleared"] = True
    except Exception as exc:
        result["release_error"] = f"{type(exc).__name__}: {exc}"
    result["gc_collected_objects"] = gc.collect()
    torch = sys.modules.get("torch")
    if empty_cuda_cache and torch is not None:
        try:
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                torch.cuda.empty_cache()
                result["cuda_empty_cache_called"] = True
        except Exception as exc:
            result["empty_cache_error"] = f"{type(exc).__name__}: {exc}"
    return result


def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    program_root = args.program_root.resolve()
    if not (program_root / "voice_terminal").is_dir():
        raise FileNotFoundError(f"源码根目录缺少 voice_terminal：{program_root}")
    sys.path.insert(0, str(program_root))
    if args.threads is not None:
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[name] = str(args.threads)
        os.environ.setdefault("VOICE_MODEL_THREADS", str(args.threads))
    logging.disable(logging.CRITICAL)
    import psutil
    sampler = Sampler(args.sample_interval)
    sampler.start()
    started = time.perf_counter()
    model = None
    output_results: list[Any] = []
    latencies: list[float] = []
    audio_path = args.audio or args.model_root / "funasr/paraformer-zh-streaming/example/asr_example.wav"
    payload: dict[str, Any] = {
        "role": args.role,
        "backend": args.backend,
        "program_root": str(program_root),
        "model_root": str(args.model_root.resolve()),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "pid": os.getpid(),
        "requested_device": args.device,
        "thread_budget": args.threads,
        "warmup_runs": args.warmup_runs,
        "iterations": args.iterations,
        "model_load_includes_internal_warmup": args.role in {"asr", "endpoint"},
        "sampling_thread_included_in_thread_count": True,
        "field_descriptions_zh": {
            "requested_device": "请求设备；实际设备请看 actual_runtime.device。",
            "actual_runtime": "模型加载后检测到的运行时、后端与实际设备；自动回退写入 load_error。",
            "actual_runtime.weight_load_mode_applied": "从实际 FunASR 权重加载函数的 __voice_model_load_mode__ 读取；不是只复述环境变量请求值。",
            "actual_runtime.funasr_module_names": "模型加载后 sys.modules 中实际加载的 FunASR（语音处理框架）模块清单。",
            "actual_runtime.funasr_module_count": "上列 FunASR 实际导入模块数量。",
            "audio.sha256": "固定 WAV（波形音频文件）原文件的 SHA-256（文件摘要）；对照两侧必须相同。",
            "audio.pcm_sha256": "解码后 PCM（脉冲编码调制音频）字节摘要；确认实际送入的音频一致。",
            "resources.samples": "按阶段记录的周期和边界采样；周期采样峰值可能漏掉短暂峰值。",
            "resources.phase_peaks": "各阶段采样最大值，不是操作系统连续追踪的硬件峰值。",
            "resources.sampled_peaks": "整个子进程期间记录到的最高资源样本。",
            "resources.stage_durations_ms": "导入、加载、显式预热、推理及释放阶段的实测累计耗时。",
            "rss_bytes": "RSS（常驻工作集）字节数，包含映射共享页；跨进程求和会重复计算共享 DLL/文件页。",
            "peak_wset_bytes": "Windows 进程从启动至当前时刻的高水位工作集；非 Windows 或接口缺失时为 null。",
            "private_commit_bytes": "Windows 私有提交量近似值；获取失败为 null，且不等同于系统总提交量。",
            "system_available_physical_bytes": "系统级可用物理内存字节数，不是当前进程专属指标。",
            "gpu_dedicated_bytes": "进程专用 GPU（图形处理器）显存计数。",
            "gpu_shared_bytes": "进程共享 GPU 内存计数，不和专用显存合并。",
            "torch_allocated_bytes": "PyTorch（深度学习框架）当前张量已分配显存。",
            "torch_reserved_bytes": "PyTorch 缓存分配器保留显存；可能高于已分配显存。",
            "torch_num_threads": "PyTorch 实际 CPU 线程数，不保证等于传入的线程预算。",
            "torch_num_interop_threads": "PyTorch 实际算子间线程数，不保证等于传入的线程预算。",
            "device_request_satisfied": "若明确请求 CUDA，只有模型及内部子模型均报告 CUDA 实际运行时才为 true；否则不把回退当成功。",
            "outputs_raw": "各次正式推理原始返回值，用于审查输出质量。",
            "semantic_outputs": "归一后的角色语义输出；精修包含文本、语言、情绪和事件标签。",
            "output_sha256": "正式推理返回值的规范化 JSON（数据交换格式）摘要。",
            "semantic_output_sha256": "语义摘要字段的摘要。",
            "release": "清空模型/会话引用与垃圾回收后的实测释放过程；不会重置 CUDA 上下文。",
        },
    }
    try:
        sampler.phase("audio_import")
        import numpy as np
        import soundfile as sf
        sampler.phase("audio_load")
        raw_file = audio_path.read_bytes()
        pcm_array, sample_rate = sf.read(audio_path, dtype="int16", always_2d=False)
        if sample_rate != 16000:
            raise ValueError(f"固定音频需为 16000 Hz，实际是 {sample_rate}")
        if getattr(pcm_array, "ndim", 1) > 1:
            raise ValueError("固定音频需为单声道，避免通道混排改变推理输入")
        pcm = pcm_array.tobytes()
        payload["audio"] = {"path": str(audio_path.resolve()), "sha256": hashlib.sha256(raw_file).hexdigest(),
                            "sample_rate_hz": sample_rate, "samples": int(len(pcm_array)),
                            "duration_seconds": len(pcm_array) / sample_rate,
                            "pcm_sha256": hashlib.sha256(pcm).hexdigest()}
        sampler.phase("model_import")
        # _build_model 会细化 model_import、model_load 和显式预热阶段。
        model, run = _build_model(args, np, pcm, sampler)
        actual = _actual_device(args.role, model, args.backend)
        runtime_devices = [actual.get("device")]
        if actual.get("internal_vad_device") is not None:
            runtime_devices.append(actual["internal_vad_device"])
        request_satisfied = (args.device != "cuda" or all(
            isinstance(device, str) and device.lower().startswith("cuda") for device in runtime_devices))
        actual["device_request_satisfied"] = request_satisfied
        payload["actual_runtime"] = actual
        payload["device_request_satisfied"] = request_satisfied
        if not request_satisfied:
            payload["device_request_error"] = (
                f"请求 CUDA（图形处理器），但检测到实际设备：{runtime_devices}")
        sampler.phase("explicit_warmup", torch_device=str(actual.get("device", "cuda:0")))
        for index in range(args.warmup_runs):
            run(pcm)
        sampler.phase("inference", torch_device=str(actual.get("device", "cuda:0")))
        for _ in range(args.iterations):
            before = time.perf_counter()
            result = run(pcm)
            latencies.append((time.perf_counter() - before) * 1000)
            output_results.append(_json_value(result))
        payload["inference_ms"] = {"samples": latencies, "percentiles": _percentiles(latencies)}
        payload["outputs_raw"] = output_results
        payload["output_sha256"] = [_digest(result) for result in output_results]
        payload["semantic_outputs"] = [_semantic_signature(result, args.role) for result in output_results]
        payload["semantic_output_sha256"] = [_digest(result) for result in payload["semantic_outputs"]]
        sampler.phase("release", torch_device=str(actual.get("device", "cuda:0")))
        payload["release"] = _release_model(args.role, model, empty_cuda_cache=args.empty_cuda_cache)
        model = None
        gc.collect()
        payload["completed_seconds"] = time.perf_counter() - started
        payload["success"] = request_satisfied
        return payload
    except Exception as exc:
        payload["success"] = False
        payload["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        if model is not None:
            try:
                payload["release"] = _release_model(args.role, model, empty_cuda_cache=args.empty_cuda_cache)
            except Exception:
                pass
        return payload
    finally:
        sampler.stop()
        payload["resources"] = sampler.report()
        payload["process_end"] = {"rss_bytes": None, "private_commit_bytes": None}
        try:
            proc = psutil.Process()
            payload["process_end"]["rss_bytes"] = int(proc.memory_info().rss)
            payload["process_end"]["private_commit_bytes"] = int(proc.memory_full_info().private)
        except Exception:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实模型资源测量子进程（不访问麦克风或云端）")
    parser.add_argument("--role", choices=("vad", "speaker", "asr", "refine", "endpoint"), required=True,
                        help="模型角色：语音检测、声纹、识别、精修或结束判断")
    parser.add_argument("--program-root", type=Path, required=True, help="被测源码根目录；基线与候选分别传入")
    parser.add_argument("--model-root", type=Path, required=True, help="本地模型资产根目录")
    parser.add_argument("--audio", type=Path, help="固定 16 kHz 单声道 WAV；缺省用 Paraformer 示例音频")
    parser.add_argument("--vad-dir", type=Path, help="本地 FSMN-VAD（语音活动检测）模型目录")
    parser.add_argument("--refine-model-dir", type=Path, help="本地精修模型目录；ONNX 候选使用专属目录")
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu", help="请求设备；结果另报告实际设备")
    parser.add_argument("--backend", "--refine-backend", dest="backend", choices=("funasr", "onnx"), default="funasr",
                        help="精修/VAD 后端；ONNX 路径仅用本地资产，不下载或导出")
    parser.add_argument("--threads", type=int, default=2, help="同等的原生线程预算，并设置常见 BLAS 线程环境变量")
    parser.add_argument("--warmup-runs", type=int, default=2, help="模型加载后相同次数的固定音频显式预热")
    parser.add_argument("--iterations", type=int, default=3, help="固定音频推理轮数")
    parser.add_argument("--sample-interval", type=float, default=.25, help="常规采样间隔（秒）；阶段边界仍同步采样")
    parser.add_argument("--empty-cuda-cache", action="store_true", help="释放模型引用后另调用 torch.cuda.empty_cache")
    parser.add_argument("--output", type=Path, required=True, help="本侧 JSON 结果路径")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.threads < 1 or args.iterations < 1 or args.warmup_runs < 0 or args.sample_interval <= 0:
        raise SystemExit("线程数/推理轮数需为正数，预热次数须非负，采样间隔须为正数")
    payload = run_worker(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "role": args.role, "success": payload.get("success"),
                      "actual_runtime": payload.get("actual_runtime"),
                      "sampled_peaks": payload.get("resources", {}).get("sampled_peaks")}, ensure_ascii=False))
    return 0 if payload.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
