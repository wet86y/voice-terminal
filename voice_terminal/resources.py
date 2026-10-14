"""Resource policy / registry（资源策略与登记）：启动前配置，按所有者计量。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Callable
from collections import Counter
from contextlib import contextmanager
from .paths import AppPaths

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
THREAD_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
ROLES = {"bootstrap", "asr", "analysis", "voice", "speaker"}


@contextmanager
def model_load_footprint():
    """model_load_footprint（加载增量估算）：共享进程内不能精确拆分原生分配器。"""
    import psutil
    process = psutil.Process()
    before = process.memory_info()
    result = {"pid": process.pid, "created_at": process.create_time(),
              "basis": "load_delta",  # basis（归属依据）：包含首次导入与预热的加载增量。
              "rss_bytes": 0, "private_commit_bytes": None}
    try:
        yield result
    finally:
        after = process.memory_info()
        result["rss_bytes"] = max(0, after.rss - before.rss)
        old_private, new_private = getattr(before, "private", None), getattr(after, "private", None)
        if old_private is not None and new_private is not None:
            result["private_commit_bytes"] = max(0, new_private - old_private)


@dataclass(frozen=True)
class ResourcePolicy:
    """ResourcePolicy（资源策略）：auto（自动选择）仅在本地候选可用时启用。"""
    native_threads: dict[str, int]  # native_threads（各进程原生线程预算）。
    vad_backend: str = "auto"  # vad_backend（实时检测后端：自动、FunASR 或 ONNX）。
    shared_audio: bool = False  # shared_audio（共享音频候选开关）。
    disk_history: bool = False  # disk_history（历史落盘候选开关）。
    history_cache_bytes: int = 8 * 1024 * 1024  # history_cache_bytes（历史读取缓存字节预算）。

    @classmethod
    def load(cls, path: Path | None = None) -> "ResourcePolicy":
        managed_paths = None
        if path is None:
            configured = os.environ.get("VOICE_RESOURCE_POLICY")
            if configured:
                path = Path(configured)
            else:
                managed_paths = AppPaths.resolve(ROOT)
                path = managed_paths.config / "runtime-resources.json"
        if managed_paths is None:
            try:
                managed_paths = AppPaths.resolve(ROOT)
            except (OSError, RuntimeError, ValueError):
                # Policy reads with explicit paths must remain independent of
                # portable-layout discovery when that metadata is unavailable.
                managed_paths = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls({})
        if not isinstance(data, dict):
            raise ValueError("资源策略必须是对象")
        policy_schema = data.get("policy_schema")  # policy_schema（资源策略格式版本）。
        if policy_schema is not None and (type(policy_schema) is not int or policy_schema not in {1, 2}):
            raise ValueError("policy_schema（资源策略格式版本）必须是受支持的整数版本")
        threads = data.get("native_threads", {})
        if not isinstance(threads, dict) or any(k not in ROLES or type(v) is not int or not 1 <= v <= 256 for k, v in threads.items()):
            raise ValueError("原生线程预算必须是已知组件对应的 1—256 整数")
        backend = data.get("vad_backend", "auto")
        managed_policy = (managed_paths is not None
                          and path.resolve(strict=False)
                          == (managed_paths.config / "runtime-resources.json").resolve(strict=False))
        if managed_policy and policy_schema != 2:
            # A managed file without the current policy schema has no reliable
            # explicit backend selection. Use the current CPU default without
            # rewriting user data; non-backend fields remain effective.
            backend = "auto"
        if backend not in {"auto", "funasr", "onnx"}:
            raise ValueError("语音检测后端必须是 auto（自动选择）、funasr（原后端）或 onnx（轻量后端）")
        for key in ("shared_audio", "disk_history"):
            if type(data.get(key, False)) is not bool:
                raise ValueError(f"{key}（候选开关）必须是布尔值")
        budget = data.get("history_cache_bytes", 8 * 1024 * 1024)
        if type(budget) is not int or budget < 0:
            raise ValueError("历史读取缓存预算必须是非负整数")
        return cls(dict(threads), backend, data.get("shared_audio", False), data.get("disk_history", False), budget)

    def environment(self, role: str, *, model_dir: Path | None = None, parent=None) -> dict[str, str]:
        env = dict(os.environ if parent is None else parent)
        paths = AppPaths.resolve()
        if paths.separated:
            env.update(paths.environment())
        env["VOICE_RESOURCE_ROLE"] = role
        env.pop("VOICE_MODEL_THREADS", None)
        if role in self.native_threads:
            env.update({name: str(self.native_threads[role]) for name in THREAD_ENV})
            env["VOICE_MODEL_THREADS"] = str(self.native_threads[role])  # VOICE_MODEL_THREADS（模型计算线程预算）。
        if model_dir:
            for name, folder in (("MODELSCOPE_CACHE", "modelscope"), ("HF_HOME", "huggingface"),
                                 ("TORCH_HOME", "torch"), ("PIP_CACHE_DIR", "pip")):
                env.setdefault(name, str(model_dir / "cache" / folder))
        return env


def bootstrap_resources(role: str) -> None:
    """先于 NumPy（数值库）等导入；直接运行工作进程也应用同一策略。"""
    os.environ.update(ResourcePolicy.load().environment(role))


def model_thread_budget(default: int = 4) -> int:
    """model_thread_budget（模型线程预算）：防止模型加载器覆盖明确选择的原生线程数。"""
    try:
        value = int(os.environ.get("VOICE_MODEL_THREADS", str(default)))
        return value if 1 <= value <= 256 else default
    except ValueError:
        return default


class ResourceRegistry:
    """ResourceRegistry（资源登记）：弱引用计量，租约显式拥有数据。"""
    def __init__(self):
        self._lock = threading.RLock()
        self._gauges: dict[str, tuple[weakref.ReferenceType, Callable]] = {}
        self._statuses: dict[str, tuple[weakref.ReferenceType, Callable]] = {}
        self._leases: dict[str, tuple[str, object, int, str]] = {}

    def observe(self, owner, purpose: str, measure: Callable) -> str:
        key = f"{purpose}:{id(owner)}"
        def gone(_):
            with self._lock:
                self._gauges.pop(key, None)
        with self._lock:
            self._gauges[key] = (weakref.ref(owner, gone), measure)
        return key

    def observe_status(self, owner, purpose: str, measure: Callable) -> None:
        key = f"{purpose}:{id(owner)}"
        def gone(_):
            with self._lock:
                self._statuses.pop(key, None)
        with self._lock:
            self._statuses[key] = (weakref.ref(owner, gone), measure)

    def lease(self, owner: str, data, size: int | None = None, *, release_on="owner_close") -> "ResourceLease":
        key = uuid.uuid4().hex
        with self._lock:
            self._leases[key] = (owner, data, len(data) if size is None else size, release_on)
        return ResourceLease(self, key)

    def release(self, key: str) -> None:
        with self._lock:
            self._leases.pop(key, None)

    def snapshot(self) -> dict:
        with self._lock:
            caches = {}
            for key, (ref, measure) in list(self._gauges.items()):
                obj = ref()
                if obj is not None:
                    try:
                        caches[key] = max(0, int(measure(obj)))
                    except (AttributeError, RuntimeError, ValueError):
                        # 计量遇到正在关闭的所有者，不得破坏原有健康检查。
                        continue
            # 同一对象被多次租用时只计一次容量。
            buffers = {id(data): size for _, data, size, _ in self._leases.values()}
            references = Counter(id(data) for _, data, _, _ in self._leases.values())
            statuses = {}
            for key, (ref, measure) in list(self._statuses.items()):
                obj = ref()
                if obj is not None:
                    statuses[key] = measure(obj)
            return {"managed_cache_bytes": sum(caches.values()),  # 受管理缓存字节数。
                    "leased_bytes": sum(buffers.values()),  # 活动租约的去重字节数。
                    "active_leases": len(self._leases),  # 活动租约数。
                    "caches": caches,  # 按用途的缓存计量。
                    "owners": statuses,  # owners（持有者状态），包含存储失败与待写材料。
                    "leases": [{"lease_id": key, "owner_pid": os.getpid(), "purpose": purpose,
                                "capacity_bytes": size, "active_references": references[id(data)],
                                "release_on": release_on}
                               for key, (purpose, data, size, release_on) in self._leases.items()]}  # 租约标识、所有者、用途、容量、引用与释放条件。


class ResourceLease:
    """ResourceLease（资源租约）：完成事件释放，不因请求取消提前释放。"""
    def __init__(self, registry: ResourceRegistry, key: str):
        self.registry, self.key = registry, key

    def close(self):
        self.registry.release(self.key)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


registry = ResourceRegistry()


def process_resource_status() -> dict:
    import psutil
    process = psutil.Process()
    memory = process.memory_info()
    from .gpu_memory import process_vram_bytes, process_vram_snapshot
    return {"pid": process.pid, "created_at": process.create_time(),  # 进程身份与创建时间。
            "gpu_dedicated_bytes": process_vram_bytes(process.pid, snapshot=process_vram_snapshot()),  # gpu_dedicated_bytes（进程专用显存字节）。
            "private_commit_bytes": getattr(memory, "private", None),  # 私有提交，非物理内存。
            "rss_bytes": memory.rss, "thread_count": process.num_threads(),  # 常驻字节数与线程数。
            "sampled_at": time.time(), "role": os.environ.get("VOICE_RESOURCE_ROLE"),  # 采样时间与角色。
            "thread_environment": {key: os.environ.get(key) for key in THREAD_ENV},  # 实际启动线程环境。
            **registry.snapshot()}


def aggregate_resource_status(rows: list[dict], *, missing_roles=()) -> dict:
    processes = {(row["pid"], row["created_at"]): row for row in rows if row.get("pid") and row.get("created_at")}
    commits = [row.get("private_commit_bytes") for row in processes.values()]
    vram = [row.get("gpu_dedicated_bytes") for row in processes.values()]
    missing = sorted(set(missing_roles))
    return {"processes": list(processes.values()),  # 去重进程清单。
            "complete": not missing, "missing_roles": missing,  # complete（覆盖完整）及 missing_roles（未采样角色）。
            "private_commit_bytes": sum(commits) if not missing and commits and all(v is not None for v in commits) else None,
            "gpu_dedicated_bytes": sum(vram) if not missing and vram and all(v is not None for v in vram) else None,  # 按进程去重的专用显存字节。
            "rss_bytes": sum(row["rss_bytes"] for row in processes.values()),
            "thread_count": sum(row["thread_count"] for row in processes.values()),
            "managed_cache_bytes": sum(row.get("managed_cache_bytes", 0) for row in processes.values()),
            "active_leases": sum(row.get("active_leases", 0) for row in processes.values())}
