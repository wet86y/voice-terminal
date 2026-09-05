from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import time
from collections.abc import Callable

from .tool_registry import FileTools


@dataclass
class ToolTask:
    task: asyncio.Task
    # Selection time is kept for compatibility with the existing overlap
    # metric.  execution_started_at is set by the worker immediately before
    # the real file operation is submitted to the executor.
    started_at: float
    execution_started_at: float | None = None
    finished_at: float | None = None


class ToolSelectionInvalid(RuntimeError):
    """The prediction that requested a tool became obsolete while selecting it."""


class SpeculativeTools:
    def __init__(self, files: FileTools) -> None:
        self.files = files
        self.entries: dict[tuple, ToolTask] = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="readonly-tool")
        self.slots = asyncio.Semaphore(2)
        self.closed = False
        self.lock = asyncio.Lock()
        self.managed = False
        self.active_turn: str | None = None

    def begin_turn(self, turn_id: str) -> None:
        self.managed = True
        self.active_turn = turn_id

    async def _execute(self, name: str, arguments: dict, mark_started: Callable[[float], None]) -> dict:
        await self.slots.acquire()
        def run_file_operation():
            mark_started(time.monotonic())
            return self.files.execute(name, arguments)
        future = asyncio.get_running_loop().run_in_executor(self.executor, run_file_operation)
        future.add_done_callback(lambda f: (self.slots.release(), f.exception() if not f.cancelled() else None))
        return await asyncio.shield(future)

    async def select(self, turn_id: str, name: str, arguments: dict, *, valid: Callable[[], bool] | None = None) -> tuple[ToolTask, bool]:
        async with self.lock:
            return await self._select(turn_id, name, arguments, valid=valid)

    async def _select(self, turn_id: str, name: str, arguments: dict, *, valid: Callable[[], bool] | None = None) -> tuple[ToolTask, bool]:
        def check_valid() -> None:
            if valid is not None and not valid():
                raise ToolSelectionInvalid("tool selection invalidated")

        if self.closed:
            raise RuntimeError("tool runtime closed")
        name, arguments = await asyncio.to_thread(self.files.normalize, name, arguments)
        if self.closed or (self.managed and self.active_turn != turn_id):
            raise RuntimeError("tool turn invalidated")
        check_valid()
        key = (turn_id, name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        entry = self.entries.get(key)
        if entry is not None:
            if not entry.task.done():
                check_valid()
                return entry, True
            if not entry.task.cancelled() and entry.task.exception() is None:
                fresh = await asyncio.to_thread(self.files.fresh, name, entry.task.result())
                if self.closed or (self.managed and self.active_turn != turn_id):
                    raise RuntimeError("tool turn invalidated")
                check_valid()
                if fresh:
                    return entry, True
        # Bound both logical backlog and retained result memory.
        if len(self.entries) >= 32 and key not in self.entries:
            removable = next((k for k, v in self.entries.items() if v.task.done()), None)
            if removable is None:
                raise RuntimeError("tool task limit reached")
            self.entries.pop(removable)
        entry_ref: list[ToolTask] = []
        async def run():
            async with asyncio.timeout(5):
                return await self._execute(name, arguments, lambda when: setattr(entry_ref[0], "execution_started_at", when))
        check_valid()
        selected_at = time.monotonic()
        entry = ToolTask(asyncio.create_task(run()), selected_at)
        entry_ref.append(entry)
        def finished(task):
            entry.finished_at = time.monotonic()
            if not task.cancelled():
                task.exception()
        entry.task.add_done_callback(finished)
        self.entries[key] = entry
        return entry, False

    def cancel_turn(self, turn_id: str) -> None:
        if self.active_turn == turn_id:
            self.active_turn = None
        for key in list(self.entries):
            if key[0] == turn_id:
                self.entries.pop(key).task.cancel()

    def close(self) -> None:
        self.closed = True
        for entry in self.entries.values():
            entry.task.cancel()
        self.entries.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
