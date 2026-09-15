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
MetricSink = Callable[[str, dict[str, Any]], None]


class _EventRelay:
    """At-least-once bridge from Shadow output to the voice heartbeat.

    The Shadow loop must not be coupled to a transient voice-side exception.
    A batch is therefore restored before retry.  Consumers use ``event_id``
    as their idempotency key because an exception can occur after part of a
    batch was already accepted.
    """

    def __init__(
        self, sink: EventSink | None, *, on_metric: MetricSink | None = None,
        retry_initial_delay_seconds: float = 0.05,
        retry_max_delay_seconds: float = 1.0,
    ) -> None:
        if retry_initial_delay_seconds <= 0 or retry_max_delay_seconds < retry_initial_delay_seconds:
            raise ValueError("invalid Shadow event relay retry delay")
        self.sink = sink
        self.on_metric = on_metric
        self.retry_initial_delay_seconds = retry_initial_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.buffer = ShadowEventBuffer()
        self.task: asyncio.Task[None] | None = None
        self.closed = False

    def _metric(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_metric is None:
            return
        try:
            self.on_metric(name, dict(payload))
        except Exception:
            pass

    def publish(self, event: ShadowEvent) -> None:
        if self.closed:
            return
        self.buffer.publish(event)
        if self.sink is not None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._flush(), name="shadow-event-relay")

    async def _flush(self) -> None:
        retry_attempt = 0
        while not self.closed:
            events = self.buffer.drain()
            if not events:
                return
            if self.sink is None:
                self.buffer.restore(events)
                return
            try:
                await self.sink(events)
            except asyncio.CancelledError:
                self.buffer.restore(events)
                raise
            except Exception as exc:
                # Do not acknowledge a batch until its sink returns.  This
                # gives terminal result/failed/clarification events a durable
                # session-owned mailbox across transient voice-side failures.
                self.buffer.restore(events)
                retry_attempt += 1
                delay = min(
                    self.retry_initial_delay_seconds * (2 ** (retry_attempt - 1)),
                    self.retry_max_delay_seconds,
                )
                self._metric("event.delivery_retry", {
                    "attempt": retry_attempt,
                    "event_count": len(events),
                    "error": type(exc).__name__,
                    "delay_ms": round(delay * 1000, 2),
                })
                await asyncio.sleep(delay)
                continue
            retry_attempt = 0
            self._metric("event.delivered", {"event_count": len(events)})

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
        on_metric = component.runtime_options.get("on_metric")
        self.relay = _EventRelay(production_sink, on_metric=on_metric if callable(on_metric) else None)
        self.runtime = ShadowRuntime(
            component.provider, component.tools, context_source,
            instructions_loader=component.instructions_loader,
            on_event=self.relay.publish,
            status_provider=component.status_provider,
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

    def reload_instructions(self) -> None:
        """Refresh Shadow persona after a product settings save/reset."""
        self.runtime.reload_instructions()

    def drain_events(self) -> list[ShadowEvent]:
        return self.relay.drain()

    async def reset(self) -> None:
        await self.runtime.reset()
        sink = self.relay.sink
        await self.relay.close()
        on_metric = self.component.runtime_options.get("on_metric")
        self.relay = _EventRelay(sink, on_metric=on_metric if callable(on_metric) else None)
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
        status_provider: ShadowProvider | None = None,
        instructions_loader: Callable[[], str], runtime_options: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.status_provider = status_provider or provider
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
        if self.status_provider is not self.provider:
            close = getattr(self.status_provider, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
