"""Ownership boundary for session-scoped Shadow Agent runtimes."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import ShadowEvent, WakeResult
from .events import ShadowEventBuffer
from .manager import ShadowContextSource, ShadowRuntime
from .provider import ShadowProvider
from .tools import ShadowToolExecutor


EventSink = Callable[[list[ShadowEvent]], Awaitable[None]]


class _EventRelay:
    def __init__(self, sink: EventSink | None) -> None:
        self.sink = sink
        self.buffer = ShadowEventBuffer()
        self.task: asyncio.Task[None] | None = None
        self.closed = False

    def publish(self, event: ShadowEvent) -> None:
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
                    # The event stays non-fatal to both execution and voice.
                    pass

    def drain(self) -> list[ShadowEvent]:
        return self.buffer.drain()

    async def close(self) -> None:
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.buffer.drain()


class ShadowAgentSession:
    """The sole Shadow runtime associated with one authoritative ledger."""

    def __init__(
        self, component: "ShadowAgentComponent", context_source: ShadowContextSource,
        *, production_sink: EventSink | None = None,
    ) -> None:
        self.component = component
        self.relay = _EventRelay(production_sink)
        self.runtime = ShadowRuntime(
            component.provider, component.tools, context_source,
            instructions_loader=component.instructions_loader,
            on_event=self.relay.publish,
            **component.runtime_options,
        )
        self.closed = False

    @property
    def available(self) -> bool:
        return not self.closed and not self.component.closed and self.runtime.available

    def wake(self) -> WakeResult:
        return self.runtime.wake()

    def snapshot(self) -> dict[str, Any]:
        return self.runtime.snapshot()

    def replace_context_source(self, context_source: ShadowContextSource) -> None:
        """Rotate the authority after a session reset while the runtime is idle."""
        if self.runtime.active:
            raise RuntimeError("cannot replace active Shadow context source")
        self.runtime.context_source = context_source

    def drain_events(self) -> list[ShadowEvent]:
        return self.relay.drain()

    async def reset(self) -> None:
        await self.runtime.reset()
        sink = self.relay.sink
        await self.relay.close()
        self.relay = _EventRelay(sink)
        self.runtime.on_event = self.relay.publish

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.runtime.close(close_tools=False)
        await self.relay.close()


class ShadowAgentComponent:
    """Reusable provider/tool owner; sessions never create task managers."""

    def __init__(
        self, provider: ShadowProvider, tools: ShadowToolExecutor, *,
        instructions_loader: Callable[[], str], runtime_options: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.instructions_loader = instructions_loader
        self.runtime_options = dict(runtime_options or {})
        self.closed = False
        self._sessions: set[ShadowAgentSession] = set()

    def new_session(
        self, context_source: ShadowContextSource, *, production_sink: EventSink | None = None,
    ) -> ShadowAgentSession:
        if self.closed:
            raise RuntimeError("shadow component closed")
        session = ShadowAgentSession(self, context_source, production_sink=production_sink)
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
