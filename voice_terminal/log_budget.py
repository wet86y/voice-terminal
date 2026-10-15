"""Log budget（日志容量预算）：日志目录总量上限与最旧优先剔除。

诊断日志属于可回收缓存，不是用户数据。这里给整个日志目录一个总量预算，
超过后按"最旧优先"逐个删除已经安静下来的轮转文件，避免长时间运行把磁盘占满。

Windows（操作系统）上被存活进程持有的文件无法删除（`PermissionError`），
这类文件跳过并记入 `skipped`，不会中断其余文件的剔除。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

DEFAULT_BUDGET_MB = 100
DEFAULT_SWEEP_SECONDS = 300.0
DEFAULT_MIN_AGE_SECONDS = 60.0

# _LOG_NAME（日志文件名模式）：只处理普通日志与轮转备份，不碰同目录的非日志文件。
_LOG_NAME = re.compile(r".+\.(?:log|jsonl)(?:\.\d+)?$", re.IGNORECASE)

logger = logging.getLogger(__name__)

_watchdog_lock = threading.Lock()
_watchdogs: dict[str, set[str]] = {}
_watchdog_running: set[str] = set()
_over_budget_reported: dict[str, int] = {}
# _global_protect（本进程持有句柄的日志文件）：任何剔除路径都不再尝试删除它们。
_global_protect: set[str] = set()


def protect_log_files(*paths: os.PathLike[str] | str) -> None:
    """登记本进程当前打开的日志文件；被其它进程持有的文件仍靠 `PermissionError` 跳过。"""
    for item in paths:
        if item:
            _global_protect.add(_key(item))


def unprotect_log_files(*paths: os.PathLike[str] | str) -> None:
    """句柄关闭后撤销登记，避免文件被永久排除在剔除范围之外。"""
    for item in paths:
        if item:
            _global_protect.discard(_key(item))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def log_budget_bytes() -> int:
    """log_budget_bytes（日志目录总预算字节）：`VOICE_LOG_BUDGET_MB=0` 关闭剔除。"""
    raw = os.environ.get("VOICE_LOG_BUDGET_MB")
    if raw is None or not raw.strip():
        return DEFAULT_BUDGET_MB * 1024 * 1024
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_MB * 1024 * 1024
    if value <= 0:
        return 0
    return int(value * 1024 * 1024)


def log_budget_sweep_seconds() -> float:
    return _env_float("VOICE_LOG_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS)


def log_budget_min_age_seconds() -> float:
    return _env_float("VOICE_LOG_MIN_AGE_SECONDS", DEFAULT_MIN_AGE_SECONDS)


def log_budget_watchdog_enabled() -> bool:
    raw = os.environ.get("VOICE_LOG_BUDGET_WATCHDOG", "1").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def _key(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def iter_log_files(log_dir: os.PathLike[str] | str) -> list[Path]:
    """iter_log_files（候选日志文件）：`*.log`/`*.jsonl` 及其数字轮转备份。"""
    try:
        entries = list(Path(log_dir).iterdir())
    except OSError:
        return []
    result: list[Path] = []
    for entry in entries:
        if not _LOG_NAME.match(entry.name):
            continue
        try:
            if entry.is_file():
                result.append(entry)
        except OSError:
            continue
    return result


def enforce_log_budget(log_dir: os.PathLike[str] | str, *, budget_bytes: int | None = None,
                       protect: Iterable[os.PathLike[str] | str] = (),
                       min_age_seconds: float | None = None,
                       quiet: bool = False) -> dict[str, Any]:
    """把日志目录总量压回预算内；返回本次剔除结果，供调用方记录或测试断言。"""

    budget = log_budget_bytes() if budget_bytes is None else int(budget_bytes)
    result: dict[str, Any] = {
        "budget_bytes": budget, "total_before": 0, "total_after": 0,
        "removed": [], "skipped": [], "over_budget": False, "disabled": budget <= 0,
    }
    if budget <= 0:
        return result
    protect_keys = {_key(item) for item in protect} | _global_protect
    min_age = log_budget_min_age_seconds() if min_age_seconds is None else float(min_age_seconds)
    now = time.time()
    entries: list[tuple[float, str, Path, int]] = []
    total = 0
    for path in iter_log_files(log_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, path.name, path, stat.st_size))
        total += stat.st_size
    result["total_before"] = total
    if total <= budget:
        result["total_after"] = total
        return result
    if min_age > 0:
        entries.sort(key=lambda item: (0 if now - item[0] >= min_age else 1, item[0], item[1]))
    else:
        entries.sort(key=lambda item: (item[0], item[1]))
    for mtime, name, path, size in entries:
        if total <= budget:
            break
        if _key(path) in protect_keys:
            result["skipped"].append(name)
            continue
        if min_age > 0 and now - mtime < min_age:
            result["skipped"].append(name)
            continue
        try:
            path.unlink()
        except OSError:
            # 被存活进程持有（Windows）或已被其他进程删除。
            result["skipped"].append(name)
            continue
        total -= size
        result["removed"].append(name)
    result["total_after"] = total
    result["over_budget"] = total > budget
    if not quiet:
        if result["removed"]:
            logger.info(
                "log_budget.sweep dir=%s removed=%d total_before_mb=%.1f total_after_mb=%.1f budget_mb=%.1f",
                log_dir, len(result["removed"]), result["total_before"] / 1048576,
                result["total_after"] / 1048576, budget / 1048576,
            )
        if result["over_budget"]:
            # 同一总量只告警一次，避免周期巡检刷屏。
            marker = _key(log_dir)
            if _over_budget_reported.get(marker) != total:
                _over_budget_reported[marker] = total
                logger.warning(
                    "log_budget.over_budget dir=%s total_mb=%.1f budget_mb=%.1f skipped=%d "
                    "reason=files_held_open_or_too_recent",
                    log_dir, total / 1048576, budget / 1048576, len(result["skipped"]),
                )
        else:
            _over_budget_reported.pop(_key(log_dir), None)
    return result


def rotate_oversized_log(path: os.PathLike[str] | str, *, max_bytes: int,
                         backups: int = 1) -> bool:
    """轮转超过上限的追加式日志（原生进程 stdout、子进程 stdout/err）。

    这些文件由其它进程持有，运行期间无法收缩，只能在下次打开前轮转。
    返回是否实际发生轮转；被占用时返回 False 且不改动文件。
    """

    target = Path(path)
    try:
        if target.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    for index in range(max(1, int(backups)), 0, -1):
        destination = target.with_name(f"{target.name}.{index}")
        source = target if index == 1 else target.with_name(f"{target.name}.{index - 1}")
        try:
            if index == backups:
                destination.unlink(missing_ok=True)
            if source.is_file():
                source.replace(destination)
        except OSError:
            logger.debug("log_budget.rotate_skipped path=%s", target, exc_info=True)
            return False
    return True


def _watchdog_loop(log_dir: str) -> None:
    interval = log_budget_sweep_seconds()
    while True:
        time.sleep(interval)
        try:
            enforce_log_budget(log_dir, protect=_watchdogs.get(_key(log_dir), ()))
        except Exception:  # 巡检永远不能影响业务进程。
            logger.warning("log_budget.sweep_failed dir=%s", log_dir, exc_info=True)


def start_log_budget_watchdog(log_dir: os.PathLike[str] | str, *,
                              protect: Iterable[os.PathLike[str] | str] = ()) -> None:
    """每个日志目录只启动一个巡检线程；重复调用只补充受保护文件。"""
    if log_budget_bytes() <= 0 or not log_budget_watchdog_enabled():
        return
    marker = _key(log_dir)
    with _watchdog_lock:
        _watchdogs.setdefault(marker, set()).update(_key(item) for item in protect)
        if marker in _watchdog_running:
            return
        _watchdog_running.add(marker)
        threading.Thread(target=_watchdog_loop, args=(marker,),
                         name="log-budget", daemon=True).start()


class BudgetedRotatingFileHandler(RotatingFileHandler):
    """轮转文件处理器：每次轮转后顺带执行一次日志目录预算剔除。"""

    def doRollover(self) -> None:  # pragma: no cover - 依赖真实轮转
        super().doRollover()
        try:
            enforce_log_budget(Path(self.baseFilename).parent, protect=(self.baseFilename,))
        except Exception:
            logger.debug("log_budget.rollover_sweep_failed path=%s", self.baseFilename, exc_info=True)
