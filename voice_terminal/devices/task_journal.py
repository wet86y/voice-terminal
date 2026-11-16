"""Device task journal（设备任务台账）：持久化请求身份，避免断线后重复执行。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class TaskConflict(ValueError):
    """TaskConflict（任务冲突）：同一编号被用于不同设备或操作。"""


@dataclass(frozen=True)
class TaskRecord:
    """TaskRecord（任务记录）：只含固定目标、摘要与最终结果。"""

    request_id: str
    device_id: str
    operation: str
    request_digest: str
    run_id: str
    status: str
    result: Any


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip():
        raise ValueError(f"{label}必须为 1 至 128 个非空字符，首尾不能有空白。")
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("任务参数或结果不是有效 JSON（结构化数据）。") from exc


class DeviceTaskJournal:
    """DeviceTaskJournal（设备任务台账）：同一编号和内容只受理一次。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # 路径由设备管理器决定；本类不创建目录或迁移用户资料。
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS device_tasks (
                request_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, operation TEXT NOT NULL,
                request_digest TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, updated_at REAL NOT NULL
            )""")
            # This auxiliary identity table is additive: existing task rows and their result meanings remain unchanged.
            db.execute("""CREATE TABLE IF NOT EXISTS device_task_remote_identity (
                request_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, run_id TEXT NOT NULL,
                connection_generation INTEGER NOT NULL, executor_generation INTEGER NOT NULL,
                sequence INTEGER NOT NULL
            )""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return TaskRecord(row["request_id"], row["device_id"], row["operation"], row["request_digest"],
                          row["run_id"], row["status"], result)

    def begin(self, *, request_id: str, device_id: str, operation: str,
              arguments: Any, run_id: str) -> tuple[TaskRecord, bool]:
        """begin（受理任务）：持久化成功后才允许调用方执行副作用。"""
        request_id = _identifier(request_id, "请求编号")
        device_id = _identifier(device_id, "设备编号")
        operation = _identifier(operation, "操作名称")
        run_id = _identifier(run_id, "运行编号")
        serialized = _canonical({"device_id": device_id, "operation": operation, "arguments": arguments})
        if len(serialized.encode("utf-8")) > 256 * 1024:
            raise ValueError("任务参数超过允许的大小。")
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM device_tasks WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise TaskConflict("请求编号已绑定其他设备、操作或参数。")
                return self._record(existing), False
            db.execute("INSERT INTO device_tasks VALUES (?, ?, ?, ?, ?, 'accepted', NULL, ?)",
                       (request_id, device_id, operation, digest, run_id, time.time()))
            row = db.execute("SELECT * FROM device_tasks WHERE request_id = ?", (request_id,)).fetchone()
            assert row is not None
            return self._record(row), True

    def get(self, request_id: str) -> TaskRecord | None:
        """get（查询任务）：不根据连接状态推断执行结果。"""
        with self._connect() as db:
            row = db.execute("SELECT * FROM device_tasks WHERE request_id = ?", (request_id,)).fetchone()
            return self._record(row) if row is not None else None

    def bind_remote_identity(self, *, request_id: str, device_id: str, run_id: str,
                             connection_generation: int, executor_generation: int,
                             sequence: int) -> bool:
        """bind_remote_identity（绑定远端代次）：将原任务与连接、执行器和序号持久关联。"""
        request_id = _identifier(request_id, "请求编号")
        device_id = _identifier(device_id, "设备编号")
        run_id = _identifier(run_id, "运行编号")
        if (any(type(value) is not int or value < 1 for value in
                (connection_generation, executor_generation, sequence))):
            raise ValueError("远端任务代次或序号无效。")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT device_id, run_id FROM device_tasks WHERE request_id = ?",
                              (request_id,)).fetchone()
            if task is None or task["device_id"] != device_id or task["run_id"] != run_id:
                raise TaskConflict("远端代次不能绑定到不存在或不匹配的原任务。")
            existing = db.execute("SELECT * FROM device_task_remote_identity WHERE request_id = ?",
                                  (request_id,)).fetchone()
            identity = (request_id, device_id, run_id, connection_generation, executor_generation, sequence)
            if existing is not None:
                current = tuple(existing[key] for key in (
                    "request_id", "device_id", "run_id", "connection_generation",
                    "executor_generation", "sequence",
                ))
                if current != identity:
                    raise TaskConflict("原任务已绑定不同的设备连接或执行器代次。")
                return True
            db.execute("INSERT INTO device_task_remote_identity VALUES (?, ?, ?, ?, ?, ?)", identity)
            return True

    def remote_identity(self, request_id: str) -> tuple[str, str, int, int, int] | None:
        """remote_identity（查询原代次）：返回设备、核心、连接/执行器代次与任务序号。"""
        request_id = _identifier(request_id, "请求编号")
        with self._connect() as db:
            row = db.execute("SELECT device_id, run_id, connection_generation, executor_generation, sequence "
                             "FROM device_task_remote_identity WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            return (row["device_id"], row["run_id"], row["connection_generation"],
                    row["executor_generation"], row["sequence"])

    def start(self, request_id: str, run_id: str) -> bool:
        """start（开始执行）：只有原运行实例可消费已受理任务。"""
        with self._connect() as db:
            cursor = db.execute("UPDATE device_tasks SET status = 'running', updated_at = ? "
                                "WHERE request_id = ? AND run_id = ? AND status = 'accepted'",
                                (time.time(), request_id, run_id))
            return cursor.rowcount == 1

    def finish(self, request_id: str, run_id: str, *, succeeded: bool, result: Any) -> bool:
        """finish（结算任务）：只结算已开始的同一运行实例。"""
        if type(succeeded) is not bool:
            raise ValueError("任务结果状态必须为布尔值。")
        encoded = _canonical(result)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("任务结果超过允许的大小。")
        with self._connect() as db:
            cursor = db.execute("UPDATE device_tasks SET status = ?, result_json = ?, updated_at = ? "
                                "WHERE request_id = ? AND run_id = ? AND status = 'running'",
                                ("succeeded" if succeeded else "failed", encoded, time.time(), request_id, run_id))
            return cursor.rowcount == 1

    def mark_unknown(self, request_id: str, run_id: str) -> bool:
        """mark_unknown（标记未知）：不推断设备是否执行了副作用。"""
        with self._connect() as db:
            cursor = db.execute("UPDATE device_tasks SET status = 'unknown', updated_at = ? "
                                "WHERE request_id = ? AND run_id = ? AND status IN ('accepted', 'running')",
                                (time.time(), request_id, run_id))
            return cursor.rowcount == 1

    def recover_stopped_owner(self, run_id: str) -> int:
        """recover_stopped_owner（恢复已停止运行实例）：仅在外部确认原实例已退出后调用。

        不推断请求是否已到达设备，也不自动重新发送。
        """
        run_id = _identifier(run_id, "运行编号")
        with self._connect() as db:
            cursor = db.execute("UPDATE device_tasks SET status = 'unknown', updated_at = ? "
                                "WHERE run_id = ? AND status IN ('accepted', 'running')",
                                (time.time(), run_id))
            return cursor.rowcount

    def resolve_unknown(self, request_id: str, *, device_id: str, request_digest: str,
                        succeeded: bool, result: Any) -> bool:
        """resolve_unknown（确认未知结果）：仅允许匹配设备和原请求摘要的回执结算。"""
        if type(succeeded) is not bool:
            raise ValueError("任务结果状态必须为布尔值。")
        encoded = _canonical(result)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("任务结果超过允许的大小。")
        with self._connect() as db:
            cursor = db.execute("UPDATE device_tasks SET status = ?, result_json = ?, updated_at = ? "
                                "WHERE request_id = ? AND device_id = ? AND request_digest = ? AND status = 'unknown'",
                                ("succeeded" if succeeded else "failed", encoded, time.time(),
                                 request_id, device_id, request_digest))
            return cursor.rowcount == 1
