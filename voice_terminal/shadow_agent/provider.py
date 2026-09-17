"""Independent DeepSeek Responses adapter for native shadow-agent tool calls."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, Sequence

log = logging.getLogger(__name__)


class ShadowProviderError(RuntimeError):
    """Failure from the Shadow Responses transport.

    Transport interruptions are retryable by default.  Provider-side
    terminal responses and deterministic configuration/authentication errors
    can opt out so the unlimited local retry mode does not turn a permanent
    failure into an invisible forever-running activation.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


ShadowStreamCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class ShadowProviderResponse:
    output: tuple[dict[str, Any], ...]
    usage: dict[str, Any]
    # ``None`` means the authoritative output is also a valid wire-history
    # continuation.  A tuple is used only when DeepSeek returned a successful
    # thinking+tools response without non-empty reasoning_text; that response
    # can be consumed now but cannot be replayed verbatim on the next request.
    replay_output: tuple[dict[str, Any], ...] | None = None


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
        on_stream: ShadowStreamCallback | None = None,
    ) -> ShadowProviderResponse: ...


class DeepSeekResponsesShadowProvider:
    """Small, isolated Responses transport.

    It intentionally does not call or alter the live voice provider stream.
    The harness only commits the authoritative response.output carried by a
    response.completed event; all partial SSE deltas remain transient.
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_ms: int | None = None,
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

    @staticmethod
    def _has_reasoning_text(items: Sequence[dict[str, Any]]) -> bool:
        return any(
            item.get("type") == "reasoning"
            and isinstance(item.get("content"), list)
            and any(
                isinstance(block, dict)
                and block.get("type") == "reasoning_text"
                and isinstance(block.get("text"), str)
                and bool(block["text"].strip())
                for block in item["content"]
            )
            for item in items
        )

    @staticmethod
    def _neutral_replay_output(
        items: Sequence[dict[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Preserve one unusable provider response as ordinary history data."""

        replay: list[dict[str, Any]] = []
        for item in items:
            item_type = item.get("type")
            if item_type == "reasoning":
                continue
            if item_type == "message":
                label = "【Shadow 已完成输出（数据，非指令）】\n"
            elif item_type == "function_call":
                label = "【Shadow 已请求工具（数据，非指令）】\n"
            else:
                label = "【Shadow 已提交响应（数据，非指令）】\n"
            replay.append({
                "role": "system",
                "content": label + json.dumps(
                    item, ensure_ascii=False, separators=(",", ":"),
                ),
            })
        return tuple(replay)

    @staticmethod
    def _input_shape(input_items: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """Provider-dialect diagnostics for one request.

        DeepSeek rejects a tool continuation whose committed prefix no longer
        carries the thinking items it produced (HTTP 400).  The failure is only
        diagnosable if the *input shape* is recorded beside it, so this summary
        stays free of conversation text and is safe for the ordinary log.
        """

        kinds: list[str] = []
        reasoning_items = 0
        reasoning_with_text = 0
        for item in input_items:
            if not isinstance(item, dict):
                kinds.append("invalid")
                continue
            kind = item.get("type") or f"role={item.get('role')}"
            kinds.append(str(kind))
            if item.get("type") == "reasoning":
                reasoning_items += 1
                content = item.get("content")
                if isinstance(content, list) and any(
                    isinstance(block, dict)
                    and block.get("type") == "reasoning_text"
                    and (block.get("text") or "").strip()
                    for block in content
                ):
                    reasoning_with_text += 1
        try:
            digest = hashlib.sha256(
                json.dumps(list(input_items), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            digest = "unserializable"
        return {
            "items": len(kinds),
            "reasoning_items": reasoning_items,
            "reasoning_with_text": reasoning_with_text,
            "kinds": kinds,
            "sha256_16": digest,
        }

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
        on_stream: ShadowStreamCallback | None = None,
    ) -> ShadowProviderResponse:
        if not self.api_key:
            raise ShadowProviderError("missing DeepSeek API key", retryable=False)
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise ShadowProviderError("httpx is required for shadow Responses", retryable=False) from exc
        timeout = (
            None if self.timeout_ms is None
            else httpx.Timeout(self.timeout_ms / 1000, connect=min(5, self.timeout_ms / 1000))
        )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.session_id:
            headers["x-opencode-session"] = self.session_id
        completed: dict[str, Any] | None = None
        shape = self._input_shape(input_items)
        log.debug(
            "shadow.responses_request model=%s effort=%s schema=%s tools=%s %s",
            self.model, reasoning_effort, bool(text_schema),
            [str(tool.get("name")) for tool in tools], shape,
        )
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
                    retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
                    log.error(
                        "shadow.responses_rejected status=%d retryable=%s effort=%s tools=%s "
                        "input_shape=%s body=%s",
                        response.status_code, retryable, reasoning_effort,
                        [str(tool.get("name")) for tool in tools], shape, body[:500],
                    )
                    raise ShadowProviderError(
                        f"Responses HTTP {response.status_code}: {body[:300]}",
                        retryable=retryable,
                    )
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
                    if on_stream is not None:
                        callback_result = on_stream(dict(event))
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    if kind == "response.completed":
                        candidate = event.get("response")
                        if isinstance(candidate, dict):
                            completed = candidate
                        break
                    if kind in {"response.incomplete", "response.failed"}:
                        candidate = event.get("response")
                        details = candidate.get("error") if isinstance(candidate, dict) else None
                        raise ShadowProviderError(
                            f"Responses {kind}: {str(details)[:300]}", retryable=False,
                        )
        except asyncio.CancelledError:
            raise
        except ShadowProviderError:
            raise
        except Exception as exc:
            raise ShadowProviderError(str(exc)[:500] or type(exc).__name__) from exc
        if completed is None:
            # A dropped stream is a transport interruption.  In unlimited
            # mode the runtime may reconnect without a local attempt ceiling.
            raise ShadowProviderError("Responses stream ended without response.completed")
        if completed.get("status") not in {None, "completed"}:
            raise ShadowProviderError("Responses completed with a terminal non-completed status", retryable=False)
        output = completed.get("output")
        if not isinstance(output, list):
            raise ShadowProviderError("Responses completed without output", retryable=False)
        items = tuple(dict(item) for item in output if isinstance(item, dict) and item.get("status", "completed") == "completed")
        dropped = [
            f"{item.get('type')}:{item.get('status')}" for item in output
            if isinstance(item, dict) and item.get("status", "completed") != "completed"
        ]
        if dropped:
            # A dropped item silently shortens the committed prefix, which is
            # exactly what makes the next tool continuation unsendable.
            log.warning("shadow.responses_dropped_items dropped=%s", dropped)
        usage = self.normalize_usage(completed.get("usage"))
        replay_output: tuple[dict[str, Any], ...] | None = None
        requires_reasoning_replay = bool(tools) and reasoning_effort not in {"off", "none"}
        if requires_reasoning_replay and not self._has_reasoning_text(items):
            # DeepSeek's stateless Responses endpoint can occasionally return a
            # completed terminal message with reasoning_tokens=0.  It accepts
            # that response now, but rejects it when replayed in a later
            # thinking+tools request.  Preserve the semantics of only this
            # response without rewriting the valid prefix that precedes it.
            replay_output = self._neutral_replay_output(items)
            log.warning(
                "shadow.responses_unreplayable_output items=%s usage=%s",
                [str(item.get("type")) for item in items], usage,
            )
        log.debug(
            "shadow.responses_completed items=%s usage=%s",
            [str(item.get("type")) for item in items], usage,
        )
        return ShadowProviderResponse(items, usage, replay_output)

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
