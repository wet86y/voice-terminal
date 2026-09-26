"""项目级定时唤醒任务（scheduled wake task）调度器，与 Voice/DSH 会话独立。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

WAKE_TARGETS = {"voice_llm", "shadow_agent", "both"}
MAX_WAKE_TEXT_CHARS = 500
MAX_WAKE_REFERENCE_CHARS = 120_000
RETRY_SECONDS = 30
_DURATION = re.compile(r"^\+?(\d+)\s*(s|m|h|d)$", re.IGNORECASE)


class WakeTaskError(ValueError):
    """A user-supplied wake task is invalid."""


def _parse_duration(value: Any, *, field: str) -> tuple[int, str] | None:
    if value is None or value == "":
        return None
    label = "时间" if field == "when" else "重复间隔"
    if not isinstance(value, str):
        raise WakeTaskError(f"{field}（{label}）必须是如 30m、2h 的固定间隔。")
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise WakeTaskError(f"{field}（{label}）请使用秒/分/小时/天，例如 30m 或 2h。")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds < 1 or seconds > 365 * 86400:
        raise WakeTaskError(f"{field}（{label}）必须在 1 秒到 365 天之间。")
    return seconds, f"{amount}{unit}"


def _parse_when(value: Any, now: datetime) -> tuple[float, str]:
    if not isinstance(value, str) or not value.strip():
        raise WakeTaskError("when（时间）必须是延迟（如 30m）或 ISO 日期时间。")
    raw = value.strip()
    if _DURATION.fullmatch(raw):
        duration = _parse_duration(raw, field="when")
        assert duration is not None
        due = now + timedelta(seconds=duration[0])
        return due.timestamp(), due.astimezone().isoformat(timespec="seconds")
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        due = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise WakeTaskError("when（时间）必须是延迟（如 30m）或 ISO 日期时间。") from exc
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    due = due.astimezone()
    return due.timestamp(), due.isoformat(timespec="seconds")


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")


def _task_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "target": row["target"],
        "when": _timestamp(row.get("scheduled_at")),
        "repeat": row.get("repeat"),
        "text": row["text"],
        "status": row["status"],
        "created_at": _timestamp(row.get("created_at")),
        "updated_at": _timestamp(row.get("updated_at")),
        "last_triggered_at": _timestamp(row.get("last_triggered_at")),
        "occurrence": int(row.get("occurrence") or 1),
        "has_reference": bool(row.get("reference")),
    }


class ScheduledWakeManager:
    """持久化唤醒规则，并通过两个运行时接口投递到期事件。"""

    def __init__(
        self,
        path: Path,
        *,
        deliver_voice: Callable[[dict[str, Any]], Awaitable[bool]],
        deliver_shadow: Callable[[dict[str, Any]], Awaitable[bool]],
    ) -> None:
        self.path = path
        self.deliver_voice = deliver_voice
        self.deliver_shadow = deliver_shadow
        self._rows = self._read()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._closed = False
        self._delivery_lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("wake_tasks.store_read_failed error=%s", type(exc).__name__)
            return {}
        rows = value.get("tasks") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(rows, dict)):
            logger.warning("wake_tasks.store_schema_unsupported")
            return {}
        return {
            str(task_id): dict(row)
            for task_id, row in rows.items()
            if isinstance(task_id, str) and isinstance(row, dict)
        }

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            {"schema_version": 1, "tasks": self._rows},
            ensure_ascii=False, sort_keys=True, indent=2,
        )
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)

    async def start(self) -> None:
        async with self._lock:
            if self._runner is not None and not self._runner.done():
                return
            self._closed = False
            now = datetime.now().astimezone().timestamp()
            changed = False
            # Repeating jobs skip every missed occurrence and retain the next
            # fixed interval; overdue one-shot jobs remain due for one delivery.
            for row in self._rows.values():
                if row.get("status") != "pending" or not row.get("repeat_seconds"):
                    continue
                interval = int(row["repeat_seconds"])
                due = float(row.get("scheduled_at") or now)
                if due <= now:
                    due += ((int((now - due) // interval) + 1) * interval)
                    row["scheduled_at"] = due
                    row["delivered_targets"] = []
                    row["retry_at"] = None
                    row["updated_at"] = now
                    changed = True
            if changed:
                self._write()
            self._runner = asyncio.create_task(self._run(), name="scheduled-wake-tasks")
        self._changed.set()

    async def close(self) -> None:
        self._closed = True
        task, self._runner = self._runner, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("target")
        if target not in WAKE_TARGETS:
            raise WakeTaskError("target（目标）只能是 voice_llm、shadow_agent 或 both。")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_WAKE_TEXT_CHARS:
            raise WakeTaskError(f"text（任务说明）不能为空且不能超过 {MAX_WAKE_TEXT_CHARS} 字符。")
        reference = payload.get("reference", "")
        if not isinstance(reference, str) or len(reference) > MAX_WAKE_REFERENCE_CHARS:
            raise WakeTaskError("reference（冻结参考）必须是有效文本，且不超过 120000 字符。")
        now = datetime.now().astimezone()
        scheduled_at, when = _parse_when(payload.get("when"), now)
        repeat_value = _parse_duration(payload.get("repeat"), field="repeat")
        row = {
            "task_id": uuid.uuid4().hex,
            "target": target,
            "scheduled_at": scheduled_at,
            "repeat_seconds": repeat_value[0] if repeat_value else None,
            "repeat": repeat_value[1] if repeat_value else None,
            "text": text.strip(),
            "reference": reference,
            "status": "pending",
            "created_at": now.timestamp(),
            "updated_at": now.timestamp(),
            "last_triggered_at": None,
            "occurrence": 1,
            "delivered_targets": [],
            "retry_at": None,
        }
        async with self._lock:
            self._rows[row["task_id"]] = row
            self._write()
        self._changed.set()
        # Keep a normalized local-time string in create responses.
        return {**_task_view(row), "when": when}

    async def list_tasks(self) -> list[dict[str, Any]]:
        async with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: (item.get("status") != "pending", -(float(item.get("created_at") or 0))))
            return [_task_view(row) for row in rows]

    async def update(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"target", "when", "repeat", "text"}
        if set(payload) - allowed:
            raise WakeTaskError("只允许修改 target（目标）、when（时间）、repeat（间隔）和 text（说明）。")
        async with self._delivery_lock:
            async with self._lock:
                row = self._rows.get(task_id)
                if row is None:
                    raise KeyError(task_id)
                if row.get("status") != "pending":
                    raise WakeTaskError("已归档或已取消的任务不能修改。")
                now = datetime.now().astimezone()
                next_row = dict(row)
                if "target" in payload:
                    if payload["target"] not in WAKE_TARGETS:
                        raise WakeTaskError("target（目标）只能是 voice_llm、shadow_agent 或 both。")
                    next_row["target"] = payload["target"]
                if "when" in payload:
                    next_row["scheduled_at"], _when = _parse_when(payload["when"], now)
                if "repeat" in payload:
                    repeat_value = _parse_duration(payload["repeat"], field="repeat")
                    next_row["repeat_seconds"] = repeat_value[0] if repeat_value else None
                    next_row["repeat"] = repeat_value[1] if repeat_value else None
                if "text" in payload:
                    text = payload["text"]
                    if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_WAKE_TEXT_CHARS:
                        raise WakeTaskError(f"text（任务说明）不能为空且不能超过 {MAX_WAKE_TEXT_CHARS} 字符。")
                    next_row["text"] = text.strip()
                next_row["delivered_targets"] = []
                next_row["retry_at"] = None
                next_row["updated_at"] = now.timestamp()
                self._rows[task_id] = next_row
                self._write()
        self._changed.set()
        return _task_view(next_row)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        async with self._delivery_lock:
            async with self._lock:
                row = self._rows.get(task_id)
                if row is None:
                    raise KeyError(task_id)
                if row.get("status") == "pending":
                    row["status"] = "cancelled"
                    row["updated_at"] = datetime.now().astimezone().timestamp()
                    row["retry_at"] = None
                    self._write()
                result = _task_view(row)
        self._changed.set()
        return result

    async def delete(self, task_id: str) -> None:
        async with self._delivery_lock:
            async with self._lock:
                if task_id not in self._rows:
                    raise KeyError(task_id)
                self._rows.pop(task_id)
                self._write()
        self._changed.set()

    async def read_reference(self, task_id: str) -> dict[str, str]:
        async with self._lock:
            row = self._rows.get(task_id)
            if row is None:
                raise KeyError(task_id)
            return {"task_id": task_id, "reference": str(row.get("reference") or "")}

    async def _next_deadline(self) -> float | None:
        async with self._lock:
            deadlines = [
                float(row.get("retry_at") or row.get("scheduled_at") or 0)
                for row in self._rows.values() if row.get("status") == "pending"
            ]
        return min(deadlines) if deadlines else None

    async def _run(self) -> None:
        try:
            while not self._closed:
                self._changed.clear()
                deadline = await self._next_deadline()
                timeout = None if deadline is None else max(0.0, deadline - datetime.now().astimezone().timestamp())
                try:
                    if timeout is None:
                        await self._changed.wait()
                    else:
                        await asyncio.wait_for(self._changed.wait(), timeout=timeout)
                    continue
                except asyncio.TimeoutError:
                    await self._deliver_due()
        except asyncio.CancelledError:
            raise

    async def _deliver_due(self) -> None:
        now = datetime.now().astimezone().timestamp()
        async with self._lock:
            due_ids = [
                task_id for task_id, row in self._rows.items()
                if row.get("status") == "pending"
                and float(row.get("retry_at") or row.get("scheduled_at") or 0) <= now
            ]
        for task_id in due_ids:
            await self._deliver_one(task_id)

    async def _deliver_one(self, task_id: str) -> None:
        async with self._delivery_lock:
            async with self._lock:
                original = self._rows.get(task_id)
                if original is None or original.get("status") != "pending":
                    return
                occurrence_due = float(original.get("scheduled_at") or 0)
                occurrence = int(original.get("occurrence") or 1)
                already = set(original.get("delivered_targets") or [])
                target = str(original["target"])
                wake = {
                    "task_id": task_id,
                    "occurrence": occurrence,
                    "occurrence_id": f"{task_id}-{occurrence}",
                    "target": target,
                    "text": str(original["text"]),
                    "continuity_document": "",
                }
            targets = ["voice_llm", "shadow_agent"] if target == "both" else [target]
            async def deliver(delivery_target: str) -> bool:
                nonlocal already
                try:
                    callback = self.deliver_voice if delivery_target == "voice_llm" else self.deliver_shadow
                    accepted = bool(await callback({**wake, "target": delivery_target}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "wake_tasks.delivery_failed task_id=%s target=%s error=%s",
                        task_id, delivery_target, type(exc).__name__,
                    )
                    accepted = False
                if not accepted:
                    return False
                already.add(delivery_target)
                async with self._lock:
                    current = self._rows.get(task_id)
                    if (current is not None and current.get("status") == "pending"
                            and int(current.get("occurrence") or 1) == occurrence
                            and float(current.get("scheduled_at") or 0) == occurrence_due):
                        current["delivered_targets"] = sorted(already)
                        current["last_triggered_at"] = datetime.now().astimezone().timestamp()
                        current["retry_at"] = None
                        current["updated_at"] = current["last_triggered_at"]
                        self._write()
                return True

            # A `both` wake is one event with two independent destinations;
            # start both deliveries together so a slow DSH launch cannot hold
            # up the Voice heartbeat (or the reverse).
            pending_targets = [delivery_target for delivery_target in targets if delivery_target not in already]
            results = await asyncio.gather(*(deliver(delivery_target) for delivery_target in pending_targets))
            all_accepted = all(results)
            async with self._lock:
                current = self._rows.get(task_id)
                if (current is None or current.get("status") != "pending"
                        or int(current.get("occurrence") or 1) != occurrence
                        or float(current.get("scheduled_at") or 0) != occurrence_due):
                    return
                if all_accepted and len(already) == len(targets):
                    if current.get("repeat_seconds"):
                        interval = int(current["repeat_seconds"])
                        next_due = occurrence_due + interval
                        now = datetime.now().astimezone().timestamp()
                        if next_due <= now:
                            next_due += ((int((now - next_due) // interval) + 1) * interval)
                        current["scheduled_at"] = next_due
                        current["occurrence"] = occurrence + 1
                        current["delivered_targets"] = []
                        current["retry_at"] = None
                        current["updated_at"] = now
                    else:
                        current["status"] = "archived"
                        current["updated_at"] = datetime.now().astimezone().timestamp()
                        current["retry_at"] = None
                    self._write()
                    logger.info("wake_tasks.handoff_accepted task_id=%s occurrence=%d", task_id, occurrence)
                else:
                    current["retry_at"] = datetime.now().astimezone().timestamp() + RETRY_SECONDS
                    current["updated_at"] = datetime.now().astimezone().timestamp()
                    self._write()
        self._changed.set()
