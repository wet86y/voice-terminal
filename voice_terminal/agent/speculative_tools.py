from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import time
from collections.abc import Callable

from .tool_registry import FileTools, WebSearchTools, TOOL_EFFECTS


@dataclass
class ToolTask:
    task: asyncio.Task
    # Selection time is kept for compatibility with the existing overlap
    # metric.  execution_started_at is set by the worker immediately before
    # the real file operation is submitted to the executor.
    started_at: float
    execution_started_at: float | None = None
    finished_at: float | None = None
    origin: dict = field(default_factory=dict)


class ToolSelectionInvalid(RuntimeError):
    """The prediction that requested a tool became obsolete while selecting it."""


class SpeculativeTools:
    def __init__(self, files: FileTools, search: WebSearchTools | None = None,
                 on_disabled: Callable[[str, str], None] | None = None,
                 on_status: Callable[[str, str, str], None] | None = None) -> None:
        self.files = files
        self.search = search
        self.on_disabled = on_disabled
        self.on_status = on_status
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

    def _backend(self, name: str):
        if name == "web_search":
            return self.search
        return self.files

    def effect(self, name: str) -> str | None:
        return TOOL_EFFECTS.get(name) if name in {s["function"]["name"] for s in self.schemas()} else None

    def schemas(self) -> list[dict]:
        schemas = self.files.schemas()
        if self.search is not None:
            schemas.extend(self.search.schemas())
        return schemas

    def is_available(self, name: str) -> bool:
        backend = self._backend(name)
        return backend is not None and getattr(backend, "available", True)

    def service_status(self, name: str) -> str:
        backend = self._backend(name)
        if backend is None:
            return "DISABLED"
        return getattr(backend, "service_status", "READY" if self.is_available(name) else "DISABLED")

    def _notify_status(self, name: str, reason: str) -> None:
        status = self.service_status(name)
        if self.on_status is not None:
            self.on_status(name, status, reason)
        elif status == "DISABLED" and self.on_disabled is not None:
            self.on_disabled(name, reason)

    def disable(self, name: str, reason: str) -> None:
        backend = self._backend(name)
        if backend is not None and hasattr(backend, "disable"):
            backend.disable(reason)
            self._notify_status(name, reason)

    def mark_error(self, name: str, reason: str) -> None:
        backend = self._backend(name)
        if backend is not None and hasattr(backend, "mark_error"):
            backend.mark_error(reason)
            self._notify_status(name, reason)

    async def _execute(self, backend, name: str, arguments: dict, mark_started: Callable[[float], None]) -> dict:
        await self.slots.acquire()
        def run_tool_operation():
            mark_started(time.monotonic())
            try:
                return backend.execute(name, arguments)
            except Exception as exc:
                if getattr(exc, "disable_search", False):
                    # The backend also records its disabled state.  Going
                    # through the registry hook makes service diagnostics
                    # update even if the visible turn is canceled meanwhile.
                    self.disable(name, str(exc))
                elif getattr(exc, "service_status", None) == "ERROR":
                    self.mark_error(name, str(exc))
                raise
        future = asyncio.get_running_loop().run_in_executor(self.executor, run_tool_operation)
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
        backend = self._backend(name)
        if backend is None or not getattr(backend, "available", True):
            raise ToolSelectionInvalid("tool unavailable")
        name, arguments = await asyncio.to_thread(backend.normalize, name, arguments)
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
                fresh = await asyncio.to_thread(backend.fresh, name, entry.task.result())
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
                return await self._execute(backend, name, arguments, lambda when: setattr(entry_ref[0], "execution_started_at", when))
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
