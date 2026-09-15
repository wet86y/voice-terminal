"""Execution for the existing read-only Shadow tool backends."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Callable

from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from .contracts import ToolEffect


class ShadowToolDomain(StrEnum):
    """Capability domains are policy labels, not separate processes."""

    INTERNAL = "internal"
    EXTERNAL = "external"


class _ShadowToolTimeout(TimeoutError):
    """Internal marker for a single backend call exceeding its deadline."""


class ShadowToolExecutor:
    """Execute the single Shadow runtime's frozen READ_ONLY capabilities."""

    def __init__(self, files: FileTools | None, search: WebSearchTools | None = None, *,
                 timeout_ms: int | None = 15000,
                 max_concurrency: int | None = None,
                 on_status: Callable[[str, str, str], None] | None = None) -> None:
        if timeout_ms is not None and timeout_ms < 1:
            raise ValueError("timeout_ms must be positive or null")
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be positive or null")
        self.files = files
        self.search = search
        self.timeout_ms = timeout_ms
        self.on_status = on_status
        # Production Shadow execution is intentionally unbounded.  Keep the
        # optional semaphore only as a compatibility escape hatch for callers
        # that explicitly need a bounded test/embedding environment.
        self._slots = asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        self.closed = False
        self._detached_work: set[asyncio.Task[Any]] = set()

    def _consume_detached_work(self, work: asyncio.Task[Any]) -> None:
        self._detached_work.discard(work)
        if work.cancelled():
            return
        try:
            work.exception()
        except BaseException:
            # A timed-out read-only backend may finish with an error after its
            # public tool result has already been returned.  Consume it so it
            # cannot become an unhandled task warning.
            pass

    def _detach_work(self, work: asyncio.Task[Any]) -> None:
        self._detached_work.add(work)
        work.add_done_callback(self._consume_detached_work)

    async def _run_thread(
        self, name: str, function: Callable[..., Any], *args: Any,
        deadline: float | None = None,
    ) -> Any:
        """Run one synchronous backend operation with the tool deadline.

        ``shield`` deliberately keeps an already-started read-only thread
        alive when the timeout fires or the caller is cancelled.  A timed-out
        operation is detached and converted into a normal tool error; a reset
        still waits for an operation that has already started, preserving the
        existing file consistency guarantees.
        """
        timeout_seconds = None
        if deadline is not None:
            timeout_seconds = deadline - time.monotonic()
            if timeout_seconds <= 0:
                raise _ShadowToolTimeout(
                    f"工具 {name} 执行超过 {self.timeout_ms}ms",
                )
        elif self.timeout_ms is not None:
            timeout_seconds = self.timeout_ms / 1000
        work = asyncio.create_task(
            asyncio.to_thread(function, *args), name=f"shadow-readonly-{name}",
        )
        try:
            if timeout_seconds is None:
                return await asyncio.shield(work)
            return await asyncio.wait_for(
                asyncio.shield(work), timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._detach_work(work)
            raise _ShadowToolTimeout(
                f"工具 {name} 执行超过 {self.timeout_ms}ms",
            ) from exc
        except asyncio.CancelledError:
            # Reset/close cancellation keeps the old guarantee that an
            # already-started read-only operation reaches a safe return.
            await asyncio.gather(work, return_exceptions=True)
            raise

    def action_definitions(self, *, domain: ShadowToolDomain | None = None) -> list[dict[str, Any]]:
        if domain is not None and domain is not ShadowToolDomain.EXTERNAL:
            return []
        definitions = list(self.files.action_definitions()) if self.files is not None else []
        if self.search is not None:
            definitions.extend(self.search.action_definitions())
        return definitions

    def responses_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", **definition} for definition in self.action_definitions()]

    def capability_effects(self) -> dict[str, ToolEffect]:
        return {definition["name"]: ToolEffect.READ_ONLY for definition in self.action_definitions()}

    def assert_read_only(self) -> None:
        disallowed = [
            name for name, effect in self.capability_effects().items()
            if effect is not ToolEffect.READ_ONLY
        ]
        if disallowed:
            raise ValueError(f"non-read-only Shadow tools are disabled: {', '.join(disallowed)}")

    def _backend(self, name: str) -> Any:
        if name == "web_search":
            return self.search
        if name in {"files_search", "files_read_text"}:
            return self.files
        return None

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        if self.closed:
            return {"error": "shadow tool executor closed"}
        backend = self._backend(name)
        if backend is None or not getattr(backend, "available", True):
            return {"error": f"tool unavailable: {name}"}
        if not isinstance(arguments, dict):
            return {"error": f"invalid arguments for {name}"}
        try:
            deadline = (
                time.monotonic() + self.timeout_ms / 1000
                if self.timeout_ms is not None else None
            )
            normalized_name, normalized_arguments = await self._run_thread(
                name, backend.normalize, name, arguments, deadline=deadline,
            )
            result = await self._run_thread(
                normalized_name, backend.execute, normalized_name, normalized_arguments,
                deadline=deadline,
            )
            return result if isinstance(result, dict) else {"result": result}
        except _ShadowToolTimeout as exc:
            if self.on_status is not None:
                try:
                    self.on_status(name, str(getattr(backend, "service_status", "READY")), str(exc))
                except Exception:
                    pass
            return {"error": str(exc)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.on_status is not None:
                try:
                    self.on_status(name, str(getattr(backend, "service_status", "READY")), str(exc)[:300])
                except Exception:
                    pass
            # Tool errors must be returned to the model as ordinary function
            # output so it can recover, without leaking traceback details.
            return {"error": str(exc)[:500] or type(exc).__name__}

    async def execute_calls(self, calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        call_list = list(calls)

        async def one(call: dict[str, Any]) -> dict[str, Any]:
            call_id = call.get("call_id")
            name = call.get("name")
            raw_arguments = call.get("arguments")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                return {"type": "function_call_output", "call_id": str(call_id or "invalid"),
                        "output": json.dumps({"error": "malformed function call"}, ensure_ascii=False)}
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = None
            result = await self.execute(name, arguments)
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            }

        return list(await asyncio.gather(*(one(call) for call in call_list)))

    def close(self) -> None:
        self.closed = True
