from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

from .log_budget import (
    BudgetedRotatingFileHandler,
    enforce_log_budget,
    protect_log_files,
    start_log_budget_watchdog,
)

# _FILE_LIMITS（逐文件轮转上限）：单文件上限之和必须远小于日志目录总预算，
# 因为被存活进程打开的文件在 Windows（操作系统）上无法删除。
_FILE_LIMITS: dict[str, tuple[int, int]] = {
    "voice-server.log": (8 * 1024 * 1024, 3),
    "asr-worker.log": (4 * 1024 * 1024, 2),
    "analysis-worker.log": (4 * 1024 * 1024, 2),
    "speaker-worker.log": (4 * 1024 * 1024, 2),
}
_DEFAULT_FILE_LIMIT = (8 * 1024 * 1024, 3)
_AUDIT_FILE_LIMIT = (16 * 1024 * 1024, 3)

_context_audit_listener: QueueListener | None = None
_context_audit_handler: logging.Handler | None = None
_speaker_audit_listener: QueueListener | None = None  # 声纹控制诊断复用普通日志处理器，异步写入。


class _AuditQueueListener(QueueListener):
    """导出屏障：只等待屏障前的日志，不暂停音频线程或持续入队。"""
    def handle(self, record):
        completed = getattr(record, "_audit_flush_event", None)
        if completed is None:
            return super().handle(record)
        try:
            for handler in self.handlers:
                handler.acquire()
                try:
                    handler.flush()
                finally:
                    handler.release()
        except Exception as exc:
            record._audit_flush_error = exc
        finally:
            completed.set()


def flush_audit_logs(timeout: float = 2.0) -> None:
    """桌面导出线程调用：确保已入队的控制诊断及完整文字先落盘。"""
    deadline = time.monotonic() + timeout
    for listener in (_speaker_audit_listener, _context_audit_listener):
        if listener is None:
            continue
        marker = logging.LogRecord(__name__, logging.INFO, __file__, 0, "", (), None)
        marker._audit_flush_event = threading.Event()
        listener.queue.put_nowait(marker)
        if not marker._audit_flush_event.wait(max(0, deadline-time.monotonic())):
            raise TimeoutError("声纹与内容审计日志刷新超时")
        error = getattr(marker, "_audit_flush_error", None)
        if error is not None:
            raise error


def _stop_context_audit() -> None:
    global _context_audit_listener, _context_audit_handler, _speaker_audit_listener
    if _speaker_audit_listener is not None:
        _speaker_audit_listener.stop()
        _speaker_audit_listener = None
    if _context_audit_listener is not None:
        _context_audit_listener.stop()
        _context_audit_listener = None
    if _context_audit_handler is not None:
        _context_audit_handler.close()
        _context_audit_handler = None


atexit.register(_stop_context_audit)


def configure_logging(log_path: Path) -> None:
    """Configure console plus rotating UTF-8 file logs for a local process."""

    _stop_context_audit()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    level_name = os.environ.get("VOICE_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [pid=%(process)d]: %(message)s",
    )
    max_bytes, backup_count = _FILE_LIMITS.get(log_path.name.casefold(), _DEFAULT_FILE_LIMIT)
    file_handler = BudgetedRotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        # Keep third-party model/websocket internals at INFO while allowing
        # our own modules to emit full DEBUG diagnostics when requested.
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )
    logging.getLogger("voice_terminal").setLevel(level)
    logging.getLogger("apps").setLevel(level)
    global _speaker_audit_listener
    speaker_logger = logging.getLogger("voice_terminal.speaker.audit")
    speaker_logger.handlers.clear()
    speaker_logger.propagate = False
    speaker_logger.setLevel(logging.INFO)
    speaker_queue: queue.SimpleQueue = queue.SimpleQueue()
    speaker_logger.addHandler(QueueHandler(speaker_queue))
    _speaker_audit_listener = _AuditQueueListener(speaker_queue, file_handler, console_handler, respect_handler_level=True)
    _speaker_audit_listener.start()
    logging.getLogger(__name__).info("logging configured: path=%s level=%s", log_path, level_name)

    # Exact LLM request bodies contain conversation text and are intentionally
    # isolated from the ordinary console/server log.  Queue-backed writing
    # keeps disk rotation off the realtime event loop.  Operators can disable
    # this local diagnostic with VOICE_LLM_CONTEXT_AUDIT=0.
    global _context_audit_listener, _context_audit_handler
    audit_logger = logging.getLogger("voice_terminal.llm_context_audit")
    audit_logger.handlers.clear()
    audit_logger.propagate = False
    audit_logger.disabled = False
    audit_enabled = os.environ.get("VOICE_LLM_CONTEXT_AUDIT", "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }
    if audit_enabled:
        audit_path = log_path.with_name(f"{log_path.stem}-llm-context.jsonl")
        audit_handler = BudgetedRotatingFileHandler(
            audit_path,
            maxBytes=_AUDIT_FILE_LIMIT[0],
            backupCount=_AUDIT_FILE_LIMIT[1],
            encoding="utf-8",
        )
        audit_handler.setFormatter(logging.Formatter("%(message)s"))
        audit_queue: queue.SimpleQueue = queue.SimpleQueue()
        audit_logger.addHandler(QueueHandler(audit_queue))
        audit_logger.setLevel(logging.INFO)
        _context_audit_handler = audit_handler
        _context_audit_listener = _AuditQueueListener(audit_queue, audit_handler, respect_handler_level=True)
        _context_audit_listener.start()
        logging.getLogger(__name__).info(
            "LLM context audit enabled: path=%s contains_conversation_content=true",
            audit_path,
        )
    else:
        audit_logger.disabled = True
        logging.getLogger(__name__).info("LLM context audit disabled by VOICE_LLM_CONTEXT_AUDIT")

    # 日志目录总量预算：本进程打开的两个文件永不删除，其余按最旧优先逐步剔除。
    protected = [file_handler.baseFilename]
    if _context_audit_handler is not None:
        protected.append(_context_audit_handler.baseFilename)
    protect_log_files(*protected)
    enforce_log_budget(log_path.parent, protect=protected)
    start_log_budget_watchdog(log_path.parent, protect=protected)
