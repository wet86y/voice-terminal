"""Concurrency-safe lifecycle manager for isolated shadow tasks."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .contracts import ShadowTaskCommand, ShadowTaskEvent, TaskReceipt
from .events import ShadowTaskEventBuffer
from .prompts import CHECKPOINT_PROMPT, TIMEOUT_PROMPT, load_shadow_instructions, modification_message
from .provider import ShadowProvider, ShadowProviderError, ShadowProviderResponse
from .tools import ShadowToolExecutor


CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["completed", "text"],
    "properties": {
        "completed": {"type": "boolean"},
        "text": {"type": "string"},
    },
}
TERMINAL = {"completed", "cancelled", "timed_out", "failed"}


@dataclass
class _Task:
    id: int
    goal: str
    history: tuple[dict[str, str], ...]
    instructions: str
    input_items: list[dict[str, Any]]
    started_at: float
    work_deadline: float
    checkpoint_deadline: float
    revision: int = 0
    status: str = "running"
    checkpoint_pending: bool = False
    force_checkpoint: bool = False
    modifications: list[str] = field(default_factory=list)
    runner: asyncio.Task | None = None
    last_event: ShadowTaskEvent | None = None
    committed_steps: int = 0


class ShadowTaskManager:
    """Owns task IDs, agent runs, deadlines, revisions, and event coalescing."""

    def __init__(
        self,
        provider: ShadowProvider,
        tools: ShadowToolExecutor,
        *,
        instructions_loader: Callable[[], str] = load_shadow_instructions,
        clock: Callable[[], float] = time.monotonic,
        slice_seconds: float = 4.0,
        work_seconds: float = 30.0,
        terminal_report_seconds: float = 4.0,
        max_active_tasks: int = 8,
        on_event: Callable[[ShadowTaskEvent], None] | None = None,
    ) -> None:
        if min(slice_seconds, work_seconds, terminal_report_seconds) <= 0 or max_active_tasks < 1:
            raise ValueError("shadow task limits must be positive")
        self.provider = provider
        self.tools = tools
        self.instructions_loader = instructions_loader
        self.clock = clock
        self.slice_seconds = slice_seconds
        self.work_seconds = work_seconds
        self.terminal_report_seconds = terminal_report_seconds
        self.max_active_tasks = max_active_tasks
        self.on_event = on_event
        self._tasks: dict[int, _Task] = {}
        self._last_accepted_id = 0
        self._events = ShadowTaskEventBuffer()
        self._closed = False

    @staticmethod
    def _history(value: Sequence[dict[str, Any]] | None) -> tuple[dict[str, str], ...]:
        if value is None:
            return ()
        result: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("settled history items must be objects")
            role, content = item.get("role"), item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("settled history only accepts user/assistant text messages")
            result.append({"role": role, "content": content})
        return tuple(result)

    def _active_count(self) -> int:
        return sum(task.status == "running" for task in self._tasks.values())

    async def handle(self, command: ShadowTaskCommand | dict[str, Any], settled_history: Sequence[dict[str, Any]] | None = None) -> TaskReceipt:
        command = ShadowTaskCommand.parse(command) if isinstance(command, dict) else command
        if self._closed:
            return TaskReceipt(False, command.id, "closed", "shadow task manager closed")
        if command.state == "request":
            return await self._request(command, settled_history)
        if command.state == "modify":
            return await self._modify(command)
        return await self._cancel(command)

    async def _request(self, command: ShadowTaskCommand, settled_history: Sequence[dict[str, Any]] | None) -> TaskReceipt:
        existing = self._tasks.get(command.id)
        if existing is not None:
            if existing.status in TERMINAL:
                if existing.last_event is not None:
                    self._events.publish(existing.last_event)
                return TaskReceipt(True, command.id, existing.status, "terminal task event replayed")
            if command.text.strip():
                return TaskReceipt(False, command.id, "invalid", "active task request must use empty text; use modify")
            existing.force_checkpoint = True
            return TaskReceipt(True, command.id, "running", "checkpoint requested at next safe point")
        expected = self._last_accepted_id + 1
        if command.id != expected:
            return TaskReceipt(False, command.id, "invalid", f"next task id must be {expected}")
        if not command.text.strip():
            return TaskReceipt(False, command.id, "invalid", "new task requires nonempty text")
        if self._active_count() >= self.max_active_tasks:
            return TaskReceipt(False, command.id, "capacity", "maximum active shadow tasks reached")
        history = self._history(settled_history)
        now = self.clock()
        initial = [*history, {"role": "user", "content": f"【影子任务目标】\n{command.text.strip()}"}]
        task = _Task(
            id=command.id, goal=command.text.strip(), history=history,
            instructions=self.instructions_loader(), input_items=initial,
            started_at=now, work_deadline=now + self.work_seconds,
            checkpoint_deadline=now + self.slice_seconds,
        )
        self._tasks[task.id] = task
        self._last_accepted_id = task.id
        task.runner = asyncio.create_task(self._run(task, task.revision), name=f"shadow-task-{task.id}")
        return TaskReceipt(True, task.id, "running", "shadow task accepted")

    async def _modify(self, command: ShadowTaskCommand) -> TaskReceipt:
        task = self._tasks.get(command.id)
        if task is None or task.status in TERMINAL:
            return TaskReceipt(False, command.id, "invalid", "modify requires an active task")
        if not command.text.strip():
            return TaskReceipt(False, command.id, "invalid", "modify requires nonempty text")
        await self._interrupt(task)
        task.revision += 1
        task.modifications.append(command.text.strip())
        task.input_items.append({"role": "system", "content": modification_message(command.text)})
        task.checkpoint_pending = False
        task.force_checkpoint = False
        task.checkpoint_deadline = self.clock() + self.slice_seconds
        task.runner = asyncio.create_task(self._run(task, task.revision), name=f"shadow-task-{task.id}-r{task.revision}")
        return TaskReceipt(True, task.id, "running", "task modified and restarted")

    async def _cancel(self, command: ShadowTaskCommand) -> TaskReceipt:
        if command.text:
            return TaskReceipt(False, command.id, "invalid", "cancel requires empty text")
        task = self._tasks.get(command.id)
        if task is None:
            return TaskReceipt(True, command.id, "missing", "task does not exist")
        if task.status in TERMINAL:
            return TaskReceipt(True, command.id, task.status, "task already terminal")
        await self._interrupt(task)
        task.revision += 1
        self._terminal(task, "cancelled", "任务已取消。")
        return TaskReceipt(True, command.id, "cancelled", "task cancelled")

    async def _interrupt(self, task: _Task) -> None:
        runner = task.runner
        if runner is not None and runner is not asyncio.current_task() and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    def _current(self, task: _Task, revision: int) -> bool:
        return not self._closed and task.status == "running" and task.revision == revision

    def _publish(self, task: _Task, status: str, text: str) -> None:
        event = ShadowTaskEvent(task.id, status, text.strip() or "任务仍在处理中。")
        task.last_event = event
        self._events.publish(event)
        if self.on_event is not None:
            self.on_event(event)

    def _terminal(self, task: _Task, status: str, text: str) -> None:
        if task.status in TERMINAL:
            return
        task.status = status
        task.checkpoint_pending = False
        self._publish(task, status, text)

    @staticmethod
    def _function_calls(response: ShadowProviderResponse) -> list[dict[str, Any]]:
        return [dict(item) for item in response.output if item.get("type") == "function_call"]

    @staticmethod
    def _message_text(response: ShadowProviderResponse) -> str:
        parts: list[str] = []
        for item in response.output:
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        return "".join(parts).strip()

    @staticmethod
    def _checkpoint(response: ShadowProviderResponse) -> tuple[bool, str] | None:
        text = ShadowTaskManager._message_text(response)
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("completed"), bool) or not isinstance(value.get("text"), str):
            return None
        return value["completed"], value["text"].strip()

    async def _call_work(self, task: _Task, revision: int) -> ShadowProviderResponse:
        remaining = max(0.001, task.work_deadline - self.clock())
        return await asyncio.wait_for(self.provider.complete(
            instructions=task.instructions, input_items=tuple(task.input_items), tools=self.tools.responses_tools(),
            reasoning_effort="high",
        ), timeout=remaining)

    async def _call_checkpoint(self, task: _Task, prompt: str, timeout: float) -> tuple[bool, str] | None:
        response = await asyncio.wait_for(self.provider.complete(
            instructions=task.instructions,
            input_items=tuple([*task.input_items, {"role": "system", "content": prompt}]),
            tools=(), reasoning_effort="high", text_schema=CHECKPOINT_SCHEMA,
        ), timeout=max(0.001, timeout))
        task.input_items.extend(response.output)
        return self._checkpoint(response)

    async def _timeout(self, task: _Task, revision: int) -> None:
        if not self._current(task, revision):
            return
        fallback = task.last_event.text if task.last_event is not None else "任务在截止前没有形成可用结论。"
        try:
            result = await self._call_checkpoint(task, TIMEOUT_PROMPT, self.terminal_report_seconds)
            if result is not None and result[1]:
                fallback = result[1]
        except (asyncio.TimeoutError, ShadowProviderError, ValueError):
            pass
        if self._current(task, revision):
            self._terminal(task, "timed_out", f"任务已超时。{fallback}")

    async def _run(self, task: _Task, revision: int) -> None:
        try:
            while self._current(task, revision):
                if self.clock() >= task.work_deadline:
                    await self._timeout(task, revision)
                    return
                try:
                    response = await self._call_work(task, revision)
                except asyncio.TimeoutError:
                    await self._timeout(task, revision)
                    return
                except ShadowProviderError as exc:
                    if not self._current(task, revision):
                        return
                    self._publish(task, "running", f"任务请求暂时失败，正在重试：{str(exc)[:180]}")
                    await asyncio.sleep(0.1)
                    continue
                if not self._current(task, revision):
                    return
                task.input_items.extend(response.output)
                task.committed_steps += 1
                calls = self._function_calls(response)
                if calls:
                    try:
                        outputs = await self.tools.execute_calls(calls)
                    except asyncio.CancelledError:
                        raise
                    if not self._current(task, revision):
                        return
                    task.input_items.extend(outputs)
                    task.committed_steps += len(outputs)
                else:
                    final_text = self._message_text(response)
                    if final_text:
                        self._terminal(task, "completed", final_text)
                        return
                # A synchronous read-only tool can finish just after the
                # wall-clock deadline. Do not spend another normal checkpoint
                # or work turn after that boundary.
                if self.clock() >= task.work_deadline:
                    await self._timeout(task, revision)
                    return
                if self.clock() >= task.checkpoint_deadline:
                    task.checkpoint_pending = True
                if task.force_checkpoint:
                    task.checkpoint_pending = True
                    task.force_checkpoint = False
                if task.checkpoint_pending:
                    try:
                        checkpoint = await self._call_checkpoint(
                            task, CHECKPOINT_PROMPT, max(0.001, task.work_deadline - self.clock()),
                        )
                    except asyncio.TimeoutError:
                        await self._timeout(task, revision)
                        return
                    except ShadowProviderError as exc:
                        self._publish(task, "running", f"进度汇报暂时失败：{str(exc)[:180]}")
                        task.checkpoint_deadline = self.clock() + self.slice_seconds
                        task.checkpoint_pending = False
                        continue
                    if not self._current(task, revision):
                        return
                    task.checkpoint_pending = False
                    task.checkpoint_deadline = self.clock() + self.slice_seconds
                    if checkpoint is None:
                        self._publish(task, "running", "任务仍在处理中，进度汇报格式无效。")
                    elif checkpoint[0]:
                        self._terminal(task, "completed", checkpoint[1])
                        return
                    else:
                        self._publish(task, "running", checkpoint[1])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current(task, revision):
                self._terminal(task, "failed", f"任务执行失败：{str(exc)[:300] or type(exc).__name__}")

    def drain_events(self) -> list[ShadowTaskEvent]:
        return self._events.drain()

    def snapshot(self, identifier: int) -> dict[str, Any] | None:
        task = self._tasks.get(identifier)
        if task is None:
            return None
        return {
            "id": task.id, "status": task.status, "revision": task.revision,
            "goal": task.goal, "modifications": list(task.modifications),
            "committed_steps": task.committed_steps, "checkpoint_pending": task.checkpoint_pending,
            "last_event": task.last_event,
        }

    async def close(self, *, close_tools: bool = True) -> None:
        self._closed = True
        runners = [task.runner for task in self._tasks.values() if task.runner is not None and not task.runner.done()]
        for task in self._tasks.values():
            task.revision += 1
            if task.status == "running":
                self._terminal(task, "cancelled", "影子任务管理器已关闭。")
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        if close_tools:
            self.tools.close()
