"""统一运行操作与输入暂停租约；状态只供界面使用，不进入推理事件。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ..core.operations import OperationRegistry

logger = logging.getLogger(__name__)


@dataclass
class OperationTicket:
    key: str  # key（操作标识）。
    pause: bool  # pause（是否占用输入暂停租约）。
    phase: str  # phase（工作阶段）。
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # ready（轮到本操作）。
    started: bool = False  # started（后台协程已经进入）。
    released: bool = False  # released（租约已释放）。
    cancelled: bool = False  # cancelled（取消业务，不中断输入归还）。


class RuntimeOperations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.registry = OperationRegistry()
        self.queue: list[OperationTicket] = []
        self.restoring = False  # restoring（归还输入中）：新预约继承同一次暂停前状态。
        self.resume_requested = False  # resume_requested（暂停前的监听状态）。

    @property
    def holds(self):
        return len(self.queue)

    @property
    def closed(self):
        return bool(getattr(self.runtime, "_closed", False) or getattr(self.runtime, "_application_stopping", False))

    def intent(self, listening: bool):
        self.resume_requested = listening

    def reserve(self, target, label, *, phase="switching", pause=False):
        """同步预约，确保 202（已接纳）返回前确定先后顺序。"""
        pause = pause and hasattr(self.runtime, "_session_lock")
        key = self.registry.begin(target, label, "queued" if pause else phase)
        ticket = OperationTicket(key, pause, phase)
        if pause:
            if not self.queue and not self.restoring:
                self.resume_requested = bool(self.runtime.state.listening)
            self.queue.append(ticket)
            if len(self.queue) == 1 and not self.restoring:
                ticket.ready.set()
        return ticket

    async def publish(self):
        if getattr(self.runtime, "bus", None) is not None:
            try:
                await self.runtime.bus.publish(self.runtime.snapshot())
            except Exception:
                logger.warning("运行操作状态暂未发布", exc_info=True)

    def output_busy(self):
        state = getattr(getattr(self.runtime.state, "state", None), "value", "IDLE")
        controller = self.runtime.controller
        episode = getattr(controller, "episode", None)
        return (self.runtime.tts_switch_busy() or bool(getattr(episode, "active_request", None))
                or bool(getattr(controller, "_active_input", False))
                or state in {"FINALIZING", "THINKING", "SPEAKING", "INTERRUPTING"})

    @asynccontextmanager
    async def run(self, target, label, *, phase="switching", pause=False, wait_output=False,
                  on_release=None, ticket=None):
        ticket = ticket or self.reserve(target, label, phase=phase, pause=pause)
        ticket.started = True
        operation = RuntimeOperation(self, ticket.key)
        failure = None
        try:
            await self.publish()
            if ticket.pause:
                await ticket.ready.wait()
                if self.closed:
                    raise asyncio.CancelledError()
                await operation.update("pausing")
                async with self.runtime._session_lock:
                    if self.runtime.state.listening and self.runtime.controller is not None:
                        await self.runtime.controller.stop_listening()
                if wait_output:
                    await operation.update("waiting_output")
                    while self.output_busy():
                        if self.closed:
                            raise asyncio.CancelledError()
                        await asyncio.sleep(.05)
                await operation.update(ticket.phase)
            yield operation
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                if on_release is not None:
                    on_release()
            finally:
                cleanup = asyncio.create_task(self.finish(ticket, failure or operation.failure))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        ticket.cancelled = cancelled = True
                        if cleanup.done():
                            cleanup.result()
                            break
                if cancelled:
                    raise asyncio.CancelledError()

    async def finish(self, ticket, failure=None):
        if ticket.released:
            return
        ticket.released = True
        resume_failure = None
        try:
            if ticket.pause:
                self.queue.remove(ticket)
                if not self.queue and self.resume_requested and not self.closed and not self.runtime.state.listening:
                    self.restoring = True
                    self.registry.update(ticket.key, phase="resuming")
                    try:
                        await self.publish()
                        if not self.closed:
                            await self.runtime.start_listening()
                        if not self.closed and not self.holds and self.resume_requested and not self.runtime.state.listening:
                            raise RuntimeError("组件已更新，但监听未能恢复，请查看组件状态。")
                    except BaseException as exc:
                        failure = failure or exc
                        resume_failure = exc
                    finally:
                        self.restoring = False
            if self.closed or ticket.cancelled:
                failure = failure or asyncio.CancelledError()
            cancelled = isinstance(failure, asyncio.CancelledError)
            self.registry.update(ticket.key, state="cancelled" if cancelled else "failed" if failure else "succeeded",
                                 phase="cancelled" if cancelled else "failed" if failure else "ready",
                                 detail=(failure if isinstance(failure, str) else "操作失败，请查看组件详情。")
                                 if failure and not cancelled else "")
        finally:
            if self.queue and not self.restoring:
                self.queue[0].ready.set()
            await self.publish()
        if resume_failure is not None:
            raise resume_failure


class RuntimeOperation:
    def __init__(self, owner, key):
        self.owner, self.key, self.failure = owner, key, None

    async def update(self, phase: str, detail: str = ""):
        self.owner.registry.update(self.key, phase=phase, detail=detail)
        await self.owner.publish()

    def prevent_resume(self):
        self.owner.intent(False)

    def fail(self, detail: str):
        self.failure = detail


def runtime_operations(runtime) -> RuntimeOperations:
    value = getattr(runtime, "_runtime_operations", None)
    if value is None:
        value = runtime._runtime_operations = RuntimeOperations(runtime)
    return value


@asynccontextmanager
async def runtime_operation(runtime, target, label, **options):
    async with runtime_operations(runtime).run(target, label, **options) as operation:
        yield operation
