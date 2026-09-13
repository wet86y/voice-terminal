"""Bounded execution for the existing read-only Shadow tool backends."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Callable

from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from .contracts import ToolEffect


class ShadowToolDomain(StrEnum):
    """Capability domains are policy labels, not separate processes."""

    INTERNAL = "internal"
    EXTERNAL = "external"


class ShadowToolExecutor:
    """Execute the single Shadow runtime's frozen READ_ONLY capabilities."""

    def __init__(self, files: FileTools | None, search: WebSearchTools | None = None, *,
                 max_concurrency: int = 2,
                 on_status: Callable[[str, str, str], None] | None = None) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.files = files
        self.search = search
        self.on_status = on_status
        self._slots = asyncio.Semaphore(max_concurrency)
        self.closed = False

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
            normalized_name, normalized_arguments = await asyncio.to_thread(backend.normalize, name, arguments)
            async with self._slots:
                work = asyncio.create_task(
                    asyncio.to_thread(backend.execute, normalized_name, normalized_arguments),
                    name=f"shadow-readonly-{normalized_name}",
                )
                try:
                    result = await asyncio.shield(work)
                except asyncio.CancelledError:
                    # A reset/close stops model work, but an already-started
                    # read-only backend is allowed to reach a safe return.
                    await asyncio.gather(work, return_exceptions=True)
                    raise
            return result if isinstance(result, dict) else {"result": result}
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
