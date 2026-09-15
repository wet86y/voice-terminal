"""Independent DeepSeek Responses adapter for native shadow-agent tool calls."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


class ShadowProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowProviderResponse:
    output: tuple[dict[str, Any], ...]
    usage: dict[str, Any]


class ShadowProvider(Protocol):
    async def complete(
        self,
        *,
        instructions: str,
        input_items: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        reasoning_effort: str,
        text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> ShadowProviderResponse: ...


class DeepSeekResponsesShadowProvider:
    """Small, isolated Responses transport.

    It intentionally does not call or alter the live voice provider stream.
    The harness only commits the authoritative response.output carried by a
    response.completed event; all partial SSE deltas remain transient.
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_ms: int = 15000,
                 session_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_ms = timeout_ms
        # OpenCode Go routes Responses requests by this header.  It is kept
        # optional so direct DeepSeek remains byte-for-byte unchanged.
        self.session_id = session_id
        self._client: Any | None = None

    def set_session_id(self, session_id: str | None) -> None:
        self.session_id = session_id

    def _payload(
        self,
        *,
        instructions: str,
        input_items: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        reasoning_effort: str,
        text_schema: dict[str, Any] | None,
        max_output_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": list(input_items),
            "tools": list(tools),
            "tool_choice": "auto" if tools else "none",
            "reasoning": {
                "effort": "none" if reasoning_effort == "off" else reasoning_effort,
            },
            "stream": True,
        }
        if text_schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "shadow_checkpoint", "schema": text_schema}}
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        return payload

    async def complete(
        self,
        *,
        instructions: str,
        input_items: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        reasoning_effort: str,
        text_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> ShadowProviderResponse:
        if not self.api_key:
            raise ShadowProviderError("missing DeepSeek API key")
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise ShadowProviderError("httpx is required for shadow Responses") from exc
        timeout = httpx.Timeout(self.timeout_ms / 1000, connect=min(5, self.timeout_ms / 1000))
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.session_id:
            headers["x-opencode-session"] = self.session_id
        completed: dict[str, Any] | None = None
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=timeout)
            async with self._client.stream(
                    "POST", f"{self.base_url}/responses", headers=headers,
                    json=self._payload(
                        instructions=instructions, input_items=input_items, tools=tools,
                        reasoning_effort=reasoning_effort, text_schema=text_schema,
                        max_output_tokens=max_output_tokens,
                    ),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise ShadowProviderError(f"Responses HTTP {response.status_code}: {body[:300]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "response.completed":
                        candidate = event.get("response")
                        if isinstance(candidate, dict):
                            completed = candidate
                        break
                    if kind in {"response.incomplete", "response.failed"}:
                        candidate = event.get("response")
                        details = candidate.get("error") if isinstance(candidate, dict) else None
                        raise ShadowProviderError(f"Responses {kind}: {str(details)[:300]}")
        except asyncio.CancelledError:
            raise
        except ShadowProviderError:
            raise
        except Exception as exc:
            raise ShadowProviderError(str(exc)[:500] or type(exc).__name__) from exc
        if completed is None or completed.get("status") not in {None, "completed"}:
            raise ShadowProviderError("Responses stream ended without response.completed")
        output = completed.get("output")
        if not isinstance(output, list):
            raise ShadowProviderError("Responses completed without output")
        items = tuple(dict(item) for item in output if isinstance(item, dict) and item.get("status", "completed") == "completed")
        return ShadowProviderResponse(items, self.normalize_usage(completed.get("usage")))

    @staticmethod
    def normalize_usage(value: Any) -> dict[str, Any]:
        usage = dict(value) if isinstance(value, dict) else {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        details = usage.get("input_tokens_details")
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        cached = int(usage.get("prompt_cache_hit_tokens") or cached)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(0, input_tokens - cached))
        usage.update({
            "input_tokens": input_tokens,
            "cached_tokens": cached,
            "cache_miss_tokens": miss,
            "cache_hit_ratio": (cached / input_tokens) if input_tokens else 0.0,
        })
        return usage

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
