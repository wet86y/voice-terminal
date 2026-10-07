"""DSH startup（代理启动调度）：优先并行，物理内存达到 95% 时等待。"""
from __future__ import annotations

import asyncio

DSH_MEMORY_LIMIT_PERCENT = 95.0  # DSH_MEMORY_LIMIT_PERCENT（代理启动的物理内存使用率等待阈值）。


def startup_memory_percent() -> float | None:
    """startup_memory_percent（物理内存使用率）：采样缺失不阻止代理提前启动。"""
    import psutil
    try:
        percent = float(psutil.virtual_memory().percent)
        return percent if 0 <= percent <= 100 else None
    except (OSError, psutil.Error, ValueError, TypeError):
        return None


def begin_dsh_start(manager) -> None:
    """begin_dsh_start（提前启动代理）：复用原管理器任务、进程所有权和退出机制。"""
    if manager._closing:
        return
    if not manager.config.enabled:
        manager.begin_start()
        return
    if manager._start_task is None or manager._start_task.done():
        manager._start_task = asyncio.create_task(_start_when_memory_available(manager), name="dsh-web-start")


async def _start_when_memory_available(manager) -> None:
    if manager._process is not None and manager._process.returncode is None:
        return
    try:
        if manager.config.enabled and manager.config.auto_start:
            while not manager._closing:
                percent = startup_memory_percent()
                if percent is None or percent < DSH_MEMORY_LIMIT_PERCENT:
                    break
                manager._waiting_for_memory = True  # _waiting_for_memory（等待物理内存回落）。
                manager._state = "starting"
                manager._message = f"物理内存使用率 {percent:.1f}%，等待降至 95% 以下后启动代理。"
                await asyncio.sleep(1)
        if not manager._closing:
            manager._waiting_for_memory = False
            await manager.start()
    finally:
        manager._waiting_for_memory = False
