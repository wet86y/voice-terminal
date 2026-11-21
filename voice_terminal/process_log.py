"""Process log（子进程日志）：有界异步写入和轮转，不向业务标准输出施加背压。"""
from __future__ import annotations

import logging
from pathlib import Path
import queue
import threading

from .log_budget import BudgetedRotatingFileHandler, enforce_log_budget, protect_log_files, unprotect_log_files

logger = logging.getLogger(__name__)
_WRITER_SLOTS = threading.BoundedSemaphore(4)  # _WRITER_SLOTS（本进程诊断写线程预算）：挂起的磁盘调用也占用额度。


class _ProcessLogHandler(BudgetedRotatingFileHandler):
    def handleError(self, record):
        # 交给唯一写线程关闭故障日志；不能逐行向标准错误输出重复打印日志故障。
        raise OSError("子进程诊断日志写入失败")


class AsyncProcessLog:
    """AsyncProcessLog（异步子进程日志）：诊断可丢弃，模型协议与用户历史不经过此队列。"""

    def __init__(self, path: Path, *, max_bytes: int = 4 * 1024 * 1024,
                 backups: int = 2, queue_size: int = 256):
        self.path = path
        self.max_bytes, self.backups = max_bytes, backups
        self._queue: queue.Queue[str] = queue.Queue(maxsize=queue_size)
        self._closed = threading.Event()
        self._disabled = threading.Event()
        self.dropped_lines = 0  # dropped_lines（慢磁盘期间未写入的诊断行数）。
        self._thread = None
        if not _WRITER_SLOTS.acquire(blocking=False):
            self._disabled.set()
            logger.warning("process_log.writer_limit（诊断写线程额度已满，继续排空业务管道）")
            return
        try:
            self._thread = threading.Thread(target=self._run, name="process-log", daemon=True)
            self._thread.start()
        except BaseException:
            _WRITER_SLOTS.release()
            raise

    def write(self, text: str) -> None:
        if self._closed.is_set() or self._disabled.is_set():
            return
        try:
            self._queue.put_nowait(text if len(text) <= 8192 else text[:8192] + "…（诊断行已截断）\n")
        except queue.Full:
            self.dropped_lines += 1

    def _run(self) -> None:
        handler = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = _ProcessLogHandler(self.path, maxBytes=self.max_bytes,
                                         backupCount=self.backups, encoding="utf-8")
            protect_log_files(self.path)
            enforce_log_budget(self.path.parent, protect=(self.path,))
            while not self._closed.is_set() or not self._queue.empty():
                try:
                    text = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                record = logging.LogRecord(__name__, logging.INFO, __file__, 0,
                                           text.rstrip("\r\n"), (), None)
                handler.emit(record)
        except Exception:
            self._disabled.set()
            logger.warning("process_log.disabled path=%s action=drain_only（继续排空业务管道）", self.path, exc_info=True)
        finally:
            try:
                if handler is not None:
                    try:
                        handler.close()
                    except OSError:
                        logger.warning("process_log.close_failed（日志关闭失败） path=%s", self.path, exc_info=True)
            finally:
                unprotect_log_files(self.path)
                self._disabled.set()
                while not self._queue.empty():
                    self._queue.get_nowait()
                _WRITER_SLOTS.release()

    def stop(self) -> None:
        """stop（停止接收）：同步撤销准入，即使异步收尾再次取消，写线程仍会自行退出。"""
        self._closed.set()

    def close(self) -> None:
        """close（停止接收并限时排空）：慢磁盘不能无限阻止模型进程收尾。"""
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.dropped_lines:
            logger.warning("process_log.dropped path=%s dropped_lines（丢弃诊断行数）=%d", self.path, self.dropped_lines)
