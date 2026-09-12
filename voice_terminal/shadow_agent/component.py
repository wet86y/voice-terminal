"""Session-facing ownership for the independent shadow task runtime.

The component deliberately knows nothing about voice controllers, session
ledgers, WebSockets, or schedulers.  Callers provide narrow event sinks at
the integration boundary; the component only owns task managers, provider
state, and the shared read-only capability executor.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ShadowTaskCommand, ShadowTaskEvent, TaskReceipt
from .events import ShadowTaskEventBuffer
from .manager import ShadowTaskManager
from .provider import ShadowProvider
from .tools import ShadowToolExecutor


EventSink = Callable[[list[ShadowTaskEvent]], Awaitable[None]]


class _EventRelay:
    """Coalesce callbacks from one manager before crossing an async boundary."""

    def __init__(self, sink: EventSink | None) -> None:
        self.sink = sink
        self.buffer = ShadowTaskEventBuffer()
        self.task: asyncio.Task | None = None
        self.closed = False

    def publish(self, event: ShadowTaskEvent) -> None:
        if self.closed:
            return
        self.buffer.publish(event)
        if self.sink is not None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._flush(), name="shadow-event-relay")

    async def _flush(self) -> None:
        while not self.closed:
            events = self.buffer.drain()
            if not events:
                return
            if self.sink is not None:
                try:
                    await self.sink(events)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The owning integration can expose diagnostics. A failed
                    # observer must never crash or resurrect a task manager.
                    pass

    def drain(self) -> list[ShadowTaskEvent]:
        return self.buffer.drain()

    async def close(self) -> None:
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.buffer.drain()


@dataclass
class ShadowDebugSandbox:
    """A manager with a connection-local ID scope and event relay."""

    manager: ShadowTaskManager
    relay: _EventRelay

    async def handle(
        self,
        command: ShadowTaskCommand | dict[str, Any],
        settled_history: Sequence[dict[str, Any]] | None,
    ) -> TaskReceipt:
        return await self.manager.handle(command, settled_history)

    async def close(self) -> None:
        await self.manager.close(close_tools=False)
        await self.relay.close()


class ShadowAgentSession:
    """Conversation-scoped production manager plus isolated debug managers."""

    def __init__(self, component: "ShadowAgentComponent", *, production_sink: EventSink | None = None) -> None:
        self.component = component
        self.production_sink = production_sink
        self.production_relay = _EventRelay(production_sink)
        self.production = self._new_manager(self.production_relay.publish)
        self.debug: dict[str, ShadowDebugSandbox] = {}
        self.closed = False

    def _new_manager(self, on_event: Callable[[ShadowTaskEvent], None]) -> ShadowTaskManager:
        return ShadowTaskManager(
            self.component.provider,
            self.component.tools,
            instructions_loader=self.component.instructions_loader,
            on_event=on_event,
        )

    @property
    def available(self) -> bool:
        return not self.closed and not self.component.closed

    async def handle_voice(
        self,
        command: ShadowTaskCommand | dict[str, Any],
        settled_history: Sequence[dict[str, Any]] | None,
    ) -> TaskReceipt:
        return await self.production.handle(command, settled_history)

    async def handle_debug(
        self,
        connection_id: str,
        command: ShadowTaskCommand | dict[str, Any],
        settled_history: Sequence[dict[str, Any]] | None,
        *,
        event_sink: EventSink,
    ) -> TaskReceipt:
        sandbox = self.debug.get(connection_id)
        if sandbox is None:
            relay = _EventRelay(event_sink)
            sandbox = ShadowDebugSandbox(self._new_manager(relay.publish), relay)
            self.debug[connection_id] = sandbox
        return await sandbox.handle(command, settled_history)

    async def close_debug(self, connection_id: str) -> None:
        sandbox = self.debug.pop(connection_id, None)
        if sandbox is not None:
            await sandbox.close()

    async def reset(self) -> None:
        """Terminate every manager and create a clean production ID scope."""

        await self.production.close(close_tools=False)
        await self.production_relay.close()
        for sandbox in list(self.debug.values()):
            await sandbox.close()
        self.debug.clear()
        if not self.closed and not self.component.closed:
            self.production_relay = _EventRelay(self.production_sink)
            self.production = self._new_manager(self.production_relay.publish)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.production.close(close_tools=False)
        await self.production_relay.close()
        for sandbox in list(self.debug.values()):
            await sandbox.close()
        self.debug.clear()


class ShadowAgentComponent:
    """Reusable optional component with one shared provider and tool executor."""

    def __init__(
        self,
        provider: ShadowProvider,
        tools: ShadowToolExecutor,
        *,
        instructions_loader: Callable[[], str],
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.instructions_loader = instructions_loader
        self.closed = False
        self._sessions: set[ShadowAgentSession] = set()

    def new_session(self, *, production_sink: EventSink | None = None) -> ShadowAgentSession:
        if self.closed:
            raise RuntimeError("shadow component closed")
        session = ShadowAgentSession(self, production_sink=production_sink)
        self._sessions.add(session)
        return session

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for session in list(self._sessions):
            await session.close()
        self._sessions.clear()
        self.tools.close()
        close = getattr(self.provider, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result
