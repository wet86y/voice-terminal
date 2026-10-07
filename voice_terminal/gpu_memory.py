"""GPU memory（进程显存）：读取系统专用显存计数，不创建推理上下文。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import threading
import time


def aggregate_gpu_counters(items: list[tuple[str, int, int]]) -> dict[int, int | None]:
    """合并一个进程在多个显卡适配器上的字节计数；无效样本保留未知。"""
    result: dict[int, int | None] = {}
    for name, status, value in items:
        match = re.match(r"pid_(\d+)_", name)
        if not match:
            continue
        pid = int(match[1])
        if status not in (0, 1) or value < 0:
            result[pid] = None
        elif pid not in result or result[pid] is not None:
            result[pid] = (result.get(pid) or 0) + value
    return result


def _windows_gpu_counters() -> dict[int, int | None] | None:
    """PDH（Windows 性能计数接口）返回按进程统计的 Dedicated Usage（专用显存字节）。"""
    if os.name != "nt":
        return None
    try:
        class Values(ctypes.Union):
            _fields_ = [("largeValue", ctypes.c_longlong), ("doubleValue", ctypes.c_double), ("longValue", wintypes.LONG)]

        class Value(ctypes.Structure):
            _anonymous_ = ("values",)
            _fields_ = [("CStatus", wintypes.DWORD), ("values", Values)]

        class Item(ctypes.Structure):
            _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", Value)]

        pdh = ctypes.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        query, counter = wintypes.HANDLE(), wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            return None
        try:
            if pdh.PdhAddEnglishCounterW(query, r"\GPU Process Memory(*)\Dedicated Usage", 0, ctypes.byref(counter)) != 0:
                return None
            if pdh.PdhCollectQueryData(query) != 0:
                return None
            size, count = wintypes.DWORD(), wintypes.DWORD()
            status = pdh.PdhGetFormattedCounterArrayW(counter, 0x400, ctypes.byref(size), ctypes.byref(count), None)
            if status & 0xffffffff != 0x800007D2 or not 0 < size.value <= 16 * 1024 * 1024:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if pdh.PdhGetFormattedCounterArrayW(counter, 0x400, ctypes.byref(size), ctypes.byref(count), buffer) != 0:
                return None
            if count.value > size.value // ctypes.sizeof(Item):
                return None
            items = ctypes.cast(buffer, ctypes.POINTER(Item))
            return aggregate_gpu_counters([(items[i].szName or "", items[i].FmtValue.CStatus,
                                            items[i].FmtValue.largeValue) for i in range(count.value)])
        finally:
            pdh.PdhCloseQuery(query)
    except (OSError, AttributeError, ValueError):
        return None


_lock = threading.Lock()
_sampled_at = 0.0
_sample: dict[int, int | None] | None = None


def process_vram_snapshot() -> dict[int, int | None] | None:
    """process_vram_snapshot（进程显存快照）：一次枚举供全部组件使用，最多每两秒采样。"""
    global _sampled_at, _sample
    with _lock:
        now = time.monotonic()
        if now - _sampled_at >= 2:
            _sample = _windows_gpu_counters()
            _sampled_at = now
        return dict(_sample) if _sample is not None else None


def process_vram_bytes(pid: int, *, snapshot: dict[int, int | None] | None) -> int | None:
    """可用计数器中没有显存分配的进程为真实零；计数器不可用时返回未知。"""
    return snapshot.get(pid, 0) if snapshot is not None else None
