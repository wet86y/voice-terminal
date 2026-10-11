"""Startup diagnostics（启动诊断）：仅依赖标准库，先于模型和数值库导入。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sys
import time
import traceback

_worker_imports_started: dict[str, float] = {}
_worker_imports_finished: set[str] = set()


def initialize_worker_logging(role: str, filename: str) -> None:
    """先建立文件日志并应用线程策略，再导入业务与模型模块；父包入口可重复调用。"""
    if role in _worker_imports_started:
        return
    from .paths import AppPaths
    from .env import load_env_file
    from .logging_setup import configure_logging
    paths = AppPaths.resolve()
    load_env_file(paths.env_file)
    configure_logging(paths.logs / filename)
    from .resources import bootstrap_resources
    with startup_stage("应用工作进程资源策略"):
        bootstrap_resources(role)
    _worker_imports_started[role] = time.perf_counter()
    startup_event("工作进程业务依赖导入")


def worker_imports_ready(role: str) -> None:
    if role in _worker_imports_started and role not in _worker_imports_finished:
        _worker_imports_finished.add(role)
        startup_event("工作进程业务依赖导入", event="completed",
                      elapsed_ms=(time.perf_counter() - _worker_imports_started[role]) * 1000)


def startup_event(stage: str, *, event: str = "started", elapsed_ms: float | None = None,
                  **details) -> None:
    """写入标准错误流；受管进程将其保存到组件日志，不干扰标准输出的结果协议。"""
    payload = {"stage": stage, "event": event, "pid": os.getpid(),
               "role": os.environ.get("VOICE_RESOURCE_ROLE", "bootstrap"),
               "fields": {"stage": "启动阶段", "event": "阶段事件", "pid": "进程编号",
                          "role": "资源角色", "elapsed_ms": "阶段耗时毫秒",
                          "since_spawn_ms": "距宿主创建进程的毫秒数", "module": "入口模块",
                          "error_type": "异常类型", "files_checked": "已核验文件数量"}, **details}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = round(elapsed_ms, 1)
    try:
        spawned = float(os.environ["VOICE_PROCESS_STARTED_AT"])
        payload["since_spawn_ms"] = round(max(0, time.time() - spawned) * 1000, 1)
    except (KeyError, ValueError):
        pass
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    print(f"{stamp} startup.progress {json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)


@contextmanager
def startup_stage(stage: str):
    """记录开始、完成和异常；失败堆栈保留在同一个启动日志中。"""
    started = time.perf_counter()
    startup_event(stage)
    try:
        yield
    except BaseException as exc:
        startup_event(stage, event="failed", elapsed_ms=(time.perf_counter() - started) * 1000,
                      error_type=type(exc).__name__)
        traceback.print_exc(file=sys.stderr)
        raise
    else:
        startup_event(stage, event="completed", elapsed_ms=(time.perf_counter() - started) * 1000)
