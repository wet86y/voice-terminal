"""Startup budget（启动资源预算）：重型加载至多三路，未知资源采用串行。"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess

GIB = 1024 ** 3  # GIB（以 1024 为基数的十亿字节容量）。
MAX_MODEL_LOADS = 3  # MAX_MODEL_LOADS（同时进行重型模型加载的硬上限）。


@dataclass(frozen=True)
class LoadEstimate:
    """LoadEstimate（加载峰值预算）：包含依赖导入、模型构造和预热余量。"""
    resident_bytes: int  # resident_bytes（常驻内存预算字节）。
    commit_bytes: int  # commit_bytes（私有提交预算字节）。
    gpu_bytes: int  # gpu_bytes（显存预算字节）。


ESTIMATES = {  # estimates（首批保守加载预算），不代表模型运行时的实际占用。
    "asr": LoadEstimate(3 * GIB, 7 * GIB, 2 * GIB),
    "analysis": LoadEstimate(4 * GIB, 10 * GIB, 3 * GIB),
    # 语音分支包含关键词唤醒、语音活动检测和按配置启用的声纹子进程。
    "voice": LoadEstimate(3 * GIB, 7 * GIB, 2 * GIB),
}


@dataclass(frozen=True)
class StartupResources:
    """StartupResources（启动资源快照）：不导入推理框架、不创建显卡计算上下文。"""
    available_bytes: int | None  # available_bytes（可用物理内存字节）。
    commit_available_bytes: int | None  # commit_available_bytes（剩余系统提交内存字节）。
    logical_cpus: int  # logical_cpus（逻辑处理器数）。
    cpu_percent: float | None  # cpu_percent（系统处理器使用百分比）。
    gpu_free_bytes: int | None = None  # gpu_free_bytes（首张显卡的空闲显存字节）。


def sample_startup_resources(*, check_gpu: bool) -> StartupResources:
    """采样发生在受控后台线程；显卡查询限时且隐藏控制台窗口。"""
    import psutil
    from ..compute import system_commit_available_bytes
    try:
        memory = int(psutil.virtual_memory().available)
        cpu = psutil.cpu_percent(interval=.05)
        cpus = psutil.cpu_count() or 1
        commit = system_commit_available_bytes()
    except (OSError, psutil.Error):
        return StartupResources(None, None, 1, None)
    free = None
    if check_gpu:
        try:
            result = subprocess.run(["nvidia-smi", "--id=0", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, check=True, timeout=1,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            free = int(float(result.stdout.strip().splitlines()[0]) * 1024 * 1024)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    return StartupResources(memory, commit, cpus, cpu, free)


def parallel_admission(resources: StartupResources, pending: str, active: dict[str, tuple[int, int]],
                       *, gpu_roles: set[str], threads: dict[str, int]) -> tuple[bool, str]:
    """parallel_admission（并行放行）：已占用部分从剩余预算扣除，显存仍完整预留。"""
    if not active:
        return True, "串行加载"  # 单路始终可继续，低资源不会使启动无限排队。
    if len(active) >= MAX_MODEL_LOADS:
        return False, "加载通道已占用"
    if resources.logical_cpus < sum(threads.get(role, 4) for role in (*active, pending)):
        return False, "处理器线程预算不足"
    if resources.cpu_percent is None or resources.cpu_percent >= 80:
        return False, "处理器繁忙或采样不可用"
    estimate = ESTIMATES[pending]
    resident = estimate.resident_bytes + GIB
    commit = estimate.commit_bytes + 2 * GIB
    for role, (used_resident, used_commit) in active.items():
        resident += max(0, ESTIMATES[role].resident_bytes - used_resident)
        commit += max(0, ESTIMATES[role].commit_bytes - used_commit)
    if resources.available_bytes is None or resources.available_bytes < resident:
        return False, "可用内存不足或采样不可用"
    if resources.commit_available_bytes is None or resources.commit_available_bytes < commit:
        return False, "系统提交内存预算不足或采样不可用"
    # 同时加载多路可能使用显卡的模型时，累加全部加载分支的显存预算。
    if pending in gpu_roles and any(role in gpu_roles for role in active):
        required = estimate.gpu_bytes + GIB // 2 + sum(ESTIMATES[role].gpu_bytes for role in active if role in gpu_roles)
        if resources.gpu_free_bytes is None or resources.gpu_free_bytes < required:
            return False, "显存预算不足或采样不可用"
    return True, "资源预算允许并行加载"
