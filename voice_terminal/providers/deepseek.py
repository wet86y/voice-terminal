from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..session.context_frames import ContextFrame, ContextLane

logger = logging.getLogger(__name__)


def _safe_error_body(body: str) -> str:
    """safe_error_body（错误正文脱敏）：接口回显图片时也不记录编码。"""
    from ..screen_capture import image_summary
    import re
    try:
        return json.dumps(image_summary(json.loads(body)), ensure_ascii=False)
    except (ValueError, TypeError):
        return re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[图片编码]", body)

REASONING_EFFORTS = frozenset({"off", "low", "medium", "high", "max"})


def _canonical_reasoning_effort(value: str | None, *, default: str = "off") -> str:
    candidate = str(value or default).strip().lower()
    # ``none`` is the provider/native spelling used by the status observer;
    # the product-facing model policy uses ``off`` consistently.
    if candidate == "none":
        candidate = "off"
    if candidate not in REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be off, low, medium, high, or max"
        )
    return candidate


class DeepSeekError(RuntimeError):
    retryable = False
    error_layer = "provider"
    error_code = "PROVIDER_ERROR"


class DeepSeekHttpError(DeepSeekError):
    """HTTP failure with machine-readable retry policy."""

    error_layer = "transport"

    def __init__(self, status_code: int, body: str = "", *, retry_after: str | None = None) -> None:
        self.status_code = int(status_code)
        self.retry_after = retry_after
        self.retryable = self.status_code == 429 or 500 <= self.status_code < 600
        self.error_code = "RATE_LIMITED" if self.status_code == 429 else (
            "PROVIDER_5XX" if 500 <= self.status_code < 600 else "HTTP_ERROR"
        )
        self.body_preview = _safe_error_body(str(body))[:500]
        super().__init__(f"DeepSeek HTTP {self.status_code}: {self.body_preview}")


class DeepSeekImageReferenceError(DeepSeekHttpError):
    """ImageReferenceError（图片引用失效）：保留逻辑历史，仅允许有界传输恢复。"""
    def __init__(self, status_code: int):
        super().__init__(status_code, "已上传的图片引用失效，将重建传输引用")
        self.retryable = True
        self.error_code = "IMAGE_REFERENCE_INVALID"


class DeepSeekTransportError(DeepSeekError):
    """Connection/timeout failure which preserves whether content arrived."""

    error_layer = "transport"

    def __init__(self, kind: str, *, received_content: bool = False) -> None:
        self.transport_kind = kind
        self.received_content = bool(received_content)
        self.retryable = not self.received_content
        self.error_code = {
            "timeout": "TRANSPORT_TIMEOUT",
            "connect": "TRANSPORT_CONNECT",
        }.get(kind, "TRANSPORT_FAILURE")
        super().__init__(
            f"DeepSeek transport failure: {kind}; received_content={self.received_content}"
        )


class DeepSeekResponseTerminalError(DeepSeekError):
    """Structured non-success terminal from the Responses stream."""

    def __init__(self, kind: str, *, status: str | None = None,
                 reason: str | None = None, error_code: str | None = None,
                 stream_metrics: Mapping[str, Any] | None = None) -> None:
        self.terminal_kind = kind
        self.status = status
        self.reason = reason
        self.provider_error_code = error_code
        self.error_code = "RESPONSE_TERMINAL_FAILURE"
        self.stream_metrics = dict(stream_metrics or {})
        super().__init__(
            f"Responses terminal failure: {kind}/{status or 'unknown'}"
            f" reason={reason or 'unknown'} code={error_code or 'unknown'}"
        )


class DeepSeekClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        provider: str = "deepseek",
        api_key_env: str = "DEEPSEEK_API_KEY",
        session_id: str | None = None,
        timeout_ms: int = 30000,
        thinking: str = "disabled",
        reasoning_effort: str = "low",
        allowed_reasoning: Sequence[str] | None = None,
        transport: str = "chat_completions",
        max_output_tokens: int = 393216,
        protocol_recovery_limit: int = 1,
        transport_retry_limit: int = 2,
    ) -> None:
        if provider not in {"deepseek", "opencode_go", "openai_compatible"}:
            raise ValueError("provider must be deepseek, opencode_go, or openai_compatible")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider
        self.api_key_env = api_key_env
        self.session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        self.user_agent = "voice-terminal-backend/0.6" if provider == "opencode_go" else None
        self.timeout_ms = timeout_ms
        self.thinking = thinking
        self.reasoning_effort = _canonical_reasoning_effort(reasoning_effort)
        raw_allowed = REASONING_EFFORTS if allowed_reasoning is None else allowed_reasoning
        normalized_allowed = tuple(dict.fromkeys(
            _canonical_reasoning_effort(value) for value in raw_allowed
        ))
        if not normalized_allowed:
            raise ValueError("allowed_reasoning must contain at least one effort")
        if self.reasoning_effort not in normalized_allowed:
            raise ValueError("reasoning_effort must be included in allowed_reasoning")
        self.allowed_reasoning = frozenset(normalized_allowed)
        if transport not in {"chat_completions", "responses"}:
            raise ValueError("transport must be chat_completions or responses")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.transport = transport
        self.max_output_tokens = max_output_tokens
        self.protocol_recovery_limit = max(0, protocol_recovery_limit)
        self.transport_retry_limit = max(0, transport_retry_limit)
        self.supports_decision_schema = True
        from urllib.parse import urlsplit
        self.supports_images = self.model == "deepseek-flash" and urlsplit(self.base_url).hostname == "api.deepseek.com"  # supports_images（已验证图像协议）。
        self._client = None

    def set_session_id(self, session_id: str | None) -> None:
        """Bind subsequent provider requests to the current conversation."""
        value = session_id.strip() if isinstance(session_id, str) else ""
        self.session_id = value or None

    def supports_reasoning_effort(self, value: str | None) -> bool:
        return _canonical_reasoning_effort(value, default=self.reasoning_effort) in self.allowed_reasoning

    def _request_headers(self, *, streaming: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.session_id:
            headers["x-opencode-session"] = self.session_id
        if streaming:
            headers.update({
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            })
        return headers

    async def _http_client(self):
        if self._client is None:
            try:
                import httpx  # type: ignore
            except ImportError as exc:
                raise DeepSeekError("缺少 httpx，请先运行 scripts\\setup.ps1。") from exc
            timeout = httpx.Timeout(self.timeout_ms / 1000, connect=min(5, self.timeout_ms / 1000))
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def prepare_messages(self, messages):
        """prepare_messages（冻结视觉传输）：同一图片上传一次，后续历史复用文件引用。"""
        import asyncio
        import base64
        import copy
        import hashlib
        import time
        if not self.supports_images:
            return messages
        if not hasattr(self, "_image_sources"):
            self._image_sources = {}  # 图片摘要到稳定传输来源；不进入聊天编码存储。
            self._image_locks = {}
        if not hasattr(self, "_image_upload_limit"):
            self._image_upload_limit = asyncio.Semaphore(2)  # 最多双路上传，历史恢复不并发冲击文件接口。
        prepared = copy.deepcopy(messages)
        async def resolve(part):
            if part.get("type") != "image" or "data" not in part:
                return
            raw = base64.b64decode(part["data"], validate=True)
            key = hashlib.sha256(raw).hexdigest()
            lock = self._image_locks.setdefault(key, asyncio.Lock())
            async with lock:
                if self._image_sources.get(key, {}).get("expires_at", float("inf")) <= time.time():
                    self._image_sources.pop(key)
                if key not in self._image_sources:
                    try:
                        client = await self._http_client()
                        async with self._image_upload_limit:
                            response = await client.post(f"{self.base_url}/files", headers=self._request_headers(),
                                data={"purpose": "user_data", "expires_after[anchor]": "created_at", "expires_after[seconds]": "604800"},
                                files={"file": (f"{key}.{'png' if part['mime_type'] == 'image/png' else 'jpg'}", raw, part["mime_type"])})
                        response.raise_for_status()
                        metadata = response.json()
                        identifier = metadata.get("id")
                        if not isinstance(identifier, str) or not identifier.startswith("file-api-"):
                            raise ValueError("文件接口未返回有效图片标识")
                        expires = metadata.get("expires_at")
                        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= time.time():
                            expires = time.time() + 604800
                        self._image_sources[key] = {"file_id": identifier, "expires_at": expires}
                    except Exception as exc:
                        # 首次上传不可用则固定使用内联，不在以后每轮反复切换来源破坏前缀。
                        logger.warning("deepseek.image_inline_fallback reason=%s", type(exc).__name__)
                        self._image_sources[key] = {}
            if self._image_sources[key]:
                part["file_id"] = self._image_sources[key]["file_id"]
                part.pop("data", None)
        images = [part for frame in prepared if isinstance(frame.get("content"), list)
                  for part in frame["content"] if part.get("type") == "image"]
        await asyncio.gather(*(resolve(part) for part in images))
        return prepared

    def image_transport_snapshot(self):
        """image_transport_snapshot（图片传输检查点）：保存引用和有效期，不保存凭据或编码。"""
        import hashlib
        return {"scope": hashlib.sha256(f"{self.base_url}\0{self.api_key}".encode()).hexdigest(),
                "sources": {key: dict(value) for key, value in getattr(self, "_image_sources", {}).items()}}

    def restore_image_transport(self, snapshot):
        if hasattr(self, "_image_sources") or not isinstance(snapshot, dict):
            return
        if snapshot.get("scope") != self.image_transport_snapshot()["scope"]:
            return
        rows = snapshot.get("sources")
        if not isinstance(rows, dict):
            return
        self._image_sources = {key: dict(value) for key, value in rows.items()
            if isinstance(key, str) and len(key) == 64 and isinstance(value, dict)
            and (not value or (isinstance(value.get("file_id"), str) and value["file_id"].startswith("file-api-")
                 and isinstance(value.get("expires_at"), (float, int))))}
        self._image_locks = {}

    def _check_image_reference_error(self, messages, status, body):
        """图片被删除或失效时，重建所涉引用；逻辑消息及成品字节保持不变。"""
        text = body.casefold()
        if status not in {400, 404} or "file" not in text or not any(word in text for word in ("expired", "deleted", "not found", "not_found", "not exist", "invalid file")):
            return
        used = {part["file_id"] for message in messages if isinstance(message.get("content"), list)
                for part in message["content"] if isinstance(part, dict) and isinstance(part.get("file_id"), str)}
        if not used:
            return
        rejected = {identifier for identifier in used if identifier.casefold() in text} or used
        self._image_sources = {key: source for key, source in getattr(self, "_image_sources", {}).items() if source.get("file_id") not in rejected}
        logger.warning("deepseek.image_reference_invalid count=%s cache_rebuild=true", len(rejected))
        raise DeepSeekImageReferenceError(status)

    async def prewarm(self) -> None:
        """Warm DNS/TCP/TLS/auth transport only; never starts inference."""
        if not self.api_key:
            return
        client = await self._http_client()
        response = await client.get(f"{self.base_url}/models", headers=self._request_headers())
        if response.status_code >= 400:
            raise DeepSeekError(f"DeepSeek 预热失败：HTTP {response.status_code}")
        logger.info("deepseek.prewarm_ok status=%d", response.status_code)

    def _request_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": str(message.get("role") or "user"), "content": self._chat_content(message.get("content"))}
                for message in messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        effective_effort = _canonical_reasoning_effort(
            reasoning_effort, default=self.reasoning_effort,
        )
        if effective_effort not in self.allowed_reasoning:
            raise DeepSeekError(
                f"模型不支持思考等级 {effective_effort}；允许值为 "
                f"{', '.join(sorted(self.allowed_reasoning))}。"
            )
        thinking_enabled = effective_effort != "off" and (
            reasoning_effort is not None or self.thinking != "disabled"
        )
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled:
            payload["reasoning_effort"] = effective_effort
        return payload

    @staticmethod
    def _chat_content(content):
        """_chat_content（旧聊天接口图片格式）。"""
        if not isinstance(content, list):
            return str(content or "")
        from ..session.multimodal import responses_content
        result = []
        for part in responses_content(content):
            if part["type"] in {"input_text", "output_text"}:
                result.append({"type": "text", "text": part["text"]})
            elif "file_id" in part:
                result.append({"type": "file", "file_id": part["file_id"]})
            else:
                result.append({"type": "image_url", "image_url": {"url": part["image_url"], "detail": part.get("detail", "high")}})
        return result

    @staticmethod
    def _normalize_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize official Responses and legacy relay cache accounting."""

        normalized = dict(usage)
        if isinstance(usage.get("input_tokens"), int):
            normalized["prompt_tokens"] = usage["input_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            normalized["completion_tokens"] = usage["output_tokens"]
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
        if not isinstance(cached, int):
            cached = usage.get("prompt_cache_hit_tokens")
        if isinstance(cached, int):
            normalized["cached_tokens"] = cached
        missed = usage.get("prompt_cache_miss_tokens")
        if isinstance(missed, int):
            normalized["cache_miss_tokens"] = missed
        elif isinstance(normalized.get("prompt_tokens"), int) and isinstance(cached, int):
            normalized["cache_miss_tokens"] = max(0, normalized["prompt_tokens"] - cached)
        return normalized

    @staticmethod
    def _responses_payload(
        messages: Sequence[Mapping[str, Any]], *, model: str, schema: Mapping[str, Any],
        schema_name: str, reasoning_effort: str, max_output_tokens: int,
    ) -> dict[str, Any]:
        from ..session.multimodal import responses_content
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content") or ""
            lane = ContextFrame.lane_of(dict(message))
            if lane == ContextLane.STATIC_INSTRUCTIONS:
                if role != "system":
                    raise ValueError("static instructions must use the system role")
                instructions.append(content)
                continue
            if lane not in {ContextLane.HISTORY, ContextLane.REQUEST_TAIL}:
                raise ValueError("unsupported model context lane")
            input_items.append({
                "role": role,
                "content": responses_content(content)
                if isinstance(content, list) else [{"type": "input_text", "text": content}],
            })
        compatibility = (
            "Provider schema compatibility: every declared property is required. "
            "For wait set speak.text to the empty string; for cancel an empty text "
            "stops output and a non-empty text stops and replaces it; for revision "
            "write only the new tail. "
            "For action arguments, set fields irrelevant to that action to empty/neutral values; "
            "the local runtime will retain only that action's declared fields."
        )
        return {
            "model": model,
            "instructions": "\n\n".join([*instructions, compatibility]),
            "input": input_items,
            "reasoning": {
                "effort": "none" if _canonical_reasoning_effort(reasoning_effort) == "off"
                else _canonical_reasoning_effort(reasoning_effort),
            },
            "text": {"format": {
                "type": "json_schema", "name": schema_name,
                "strict": True, "schema": dict(schema),
            }},
            "stream": True,
            "max_output_tokens": max_output_tokens,
        }

    async def stream_chat(self, messages, *, cancel_event=None) -> AsyncIterator[str]:
        async for item in self.stream_events(messages, cancel_event=cancel_event):
            if item["type"] == "content":
                yield item["text"]

    async def stream_events(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: Any = None,
        json_mode: bool = False,
        transport: str | None = None,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "voice_decision_v06",
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict]:
        if not self.api_key:
            raise DeepSeekError(f"未配置 {self.api_key_env}")
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise DeepSeekError("缺少 httpx，请先运行 scripts\\setup.ps1。") from exc

        selected_transport = transport or self.transport
        if selected_transport == "responses":
            if schema is None:
                raise DeepSeekError("Responses transport requires a decision JSON Schema")
            selected_effort = _canonical_reasoning_effort(
                reasoning_effort,
                default=self.reasoning_effort if self.thinking != "disabled" else "off",
            )
            if selected_effort not in self.allowed_reasoning:
                raise DeepSeekError(
                    f"模型不支持思考等级 {selected_effort}；允许值为 "
                    f"{', '.join(sorted(self.allowed_reasoning))}。"
                )
            async for item in self._stream_responses(
                messages, schema=schema, schema_name=schema_name,
                reasoning_effort=selected_effort,
                cancel_event=cancel_event,
            ):
                yield item
            return
        if selected_transport != "chat_completions":
            raise DeepSeekError(f"unsupported DeepSeek transport: {selected_transport}")

        headers = self._request_headers(streaming=True)
        timeout = httpx.Timeout(self.timeout_ms / 1000, connect=min(5, self.timeout_ms / 1000))
        logger.info(
            "deepseek.request_start model=%s messages=%d timeout_ms=%d",
            self.model,
            len(messages),
            self.timeout_ms,
        )
        delta_count = 0
        response_chars = 0
        payload = self._request_payload(messages, reasoning_effort=reasoning_effort)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        finished = False
        try:
            client = await self._http_client()
            async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
            ) as response:
                logger.info("deepseek.response_status status=%d", response.status_code)
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    self._check_image_reference_error(messages, response.status_code, body)
                    logger.error("deepseek.http_error status=%d body=%r", response.status_code, _safe_error_body(body)[:500])
                    raise DeepSeekHttpError(
                        response.status_code, body,
                        retry_after=getattr(response, "headers", {}).get("Retry-After"),
                    )
                if any(isinstance(m.get("content"), list) and any(p.get("type") in {"input_image", "image_url", "file"} for p in m["content"]) for m in payload.get("input", payload.get("messages", []))):
                    yield {"type": "accepted"}  # accepted（模型HTTP接口已接受）。
                async for line in response.aiter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if not finished:
                                raise DeepSeekError("LLM stream ended without finish reason")
                            return
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = parsed.get("usage") if isinstance(parsed, dict) else None
                        if isinstance(usage, dict):
                            yield {"type": "usage", "usage": self._normalize_usage(usage)}
                        choices = parsed.get("choices") if isinstance(parsed, dict) else None
                        if not isinstance(choices, list) or not choices:
                            continue
                        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                        reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
                        if isinstance(reasoning, str) and reasoning:
                            yield {"type": "reasoning", "text": reasoning}
                        text = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(text, str) and text:
                            delta_count += 1
                            response_chars += len(text)
                            logger.debug(
                                "deepseek.delta count=%d response_chars=%d text=%r",
                                delta_count,
                                response_chars,
                                text[:120],
                            )
                            yield {"type": "content", "text": text}
                        reason = choices[0].get("finish_reason")
                        if reason is not None:
                            if reason != "stop":
                                raise DeepSeekError(f"LLM incomplete response: {reason}")
                            finished = True
                            yield {"type": "finish", "reason": reason}
                if not finished:
                    raise DeepSeekError("LLM stream disconnected before completion")
            logger.info("deepseek.request_done deltas=%d response_chars=%d", delta_count, response_chars)
        except DeepSeekError:
            raise
        except Exception as exc:
            logger.exception("deepseek.request_failed")
            kind = "timeout" if "timeout" in type(exc).__name__.casefold() else (
                "connect" if "connect" in type(exc).__name__.casefold() else "transport"
            )
            raise DeepSeekTransportError(kind, received_content=response_chars > 0) from exc

    async def _stream_responses(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        reasoning_effort: str,
        cancel_event: Any = None,
    ) -> AsyncIterator[dict]:
        headers = self._request_headers(streaming=True)
        payload = self._responses_payload(
            messages, model=self.model, schema=schema, schema_name=schema_name,
            reasoning_effort=reasoning_effort, max_output_tokens=self.max_output_tokens,
        )
        terminal = False
        streamed_parts: list[str] = []
        completed_text: str | None = None
        stream_metrics: dict[str, Any] = {
            "sse_event_count": 0,
            "sse_malformed_count": 0,
            "sequence_missing_count": 0,
            "sequence_duplicate_count": 0,
            "sequence_non_increasing_count": 0,
            "sequence_jump_count": 0,
            "first_sequence": None,
            "last_sequence": None,
        }

        def observe_sequence(event: Mapping[str, Any]) -> None:
            stream_metrics["sse_event_count"] += 1
            sequence = event.get("sequence_number")
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                stream_metrics["sequence_missing_count"] += 1
                return
            previous = stream_metrics["last_sequence"]
            if stream_metrics["first_sequence"] is None:
                stream_metrics["first_sequence"] = sequence
            if isinstance(previous, int):
                if sequence == previous:
                    stream_metrics["sequence_duplicate_count"] += 1
                elif sequence < previous:
                    stream_metrics["sequence_non_increasing_count"] += 1
                elif sequence > previous + 1:
                    # The provider documents monotonic increase, not strict
                    # contiguity. A jump is diagnostic and never fails audio.
                    stream_metrics["sequence_jump_count"] += 1
            stream_metrics["last_sequence"] = sequence

        def log_stream_metrics(*, terminal_kind: str) -> None:
            logger.info(
                "deepseek.responses_stream_integrity terminal=%s events=%d malformed=%d "
                "sequence_missing=%d sequence_duplicate=%d sequence_non_increasing=%d "
                "sequence_jump=%d first_sequence=%s last_sequence=%s",
                terminal_kind,
                stream_metrics["sse_event_count"], stream_metrics["sse_malformed_count"],
                stream_metrics["sequence_missing_count"], stream_metrics["sequence_duplicate_count"],
                stream_metrics["sequence_non_increasing_count"], stream_metrics["sequence_jump_count"],
                stream_metrics["first_sequence"], stream_metrics["last_sequence"],
            )

        def response_text(result: Any) -> str | None:
            if not isinstance(result, dict):
                return None
            parts: list[str] = []
            found = False
            output = result.get("output")
            if not isinstance(output, list):
                return None
            for item in output:
                content = item.get("content") if isinstance(item, dict) else None
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "output_text":
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        found = True
                        parts.append(text)
            return "".join(parts) if found else None
        try:
            client = await self._http_client()
            async with client.stream(
                "POST", f"{self.base_url}/responses", headers=headers, json=payload,
            ) as response:
                logger.info(
                    "deepseek.responses_status status=%d effort=%s",
                    response.status_code, reasoning_effort,
                )
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    self._check_image_reference_error(messages, response.status_code, body)
                    raise DeepSeekHttpError(
                        response.status_code, body,
                        retry_after=getattr(response, "headers", {}).get("Retry-After"),
                    )
                if any(isinstance(m.get("content"), list) and any(p.get("type") in {"input_image", "image_url"} for p in m["content"]) for m in payload.get("input", payload.get("messages", []))):
                    yield {"type": "accepted"}  # accepted（模型HTTP接口已接受）。
                async for line in response.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        stream_metrics["sse_malformed_count"] += 1
                        logger.warning("deepseek.responses_malformed_sse raw_chars=%d", len(raw))
                        continue
                    if not isinstance(event, dict):
                        stream_metrics["sse_malformed_count"] += 1
                        continue
                    observe_sequence(event)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            streamed_parts.append(delta)
                            yield {"type": "content", "text": delta}
                    elif kind == "response.output_text.done":
                        text = event.get("text")
                        if isinstance(text, str):
                            completed_text = text
                    elif kind == "response.reasoning_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield {"type": "reasoning", "text": delta}
                    elif kind == "response.completed":
                        result = event.get("response")
                        status = result.get("status") if isinstance(result, dict) else None
                        if status not in {None, "completed"}:
                            raise DeepSeekResponseTerminalError(
                                "response.completed", status=str(status),
                                reason="invalid_completed_status",
                            )
                        authoritative = completed_text
                        if authoritative is None:
                            authoritative = response_text(result)
                        streamed = "".join(streamed_parts)
                        logger.info(
                            "deepseek.responses_completed effort=%s streamed_chars=%d authoritative_chars=%s match=%s",
                            reasoning_effort, len(streamed),
                            len(authoritative) if authoritative is not None else None,
                            authoritative is None or authoritative == streamed,
                        )
                        if authoritative is not None and authoritative != streamed:
                            if authoritative.startswith(streamed):
                                suffix = authoritative[len(streamed):]
                                if suffix:
                                    yield {"type": "content", "text": suffix}
                            else:
                                raise DeepSeekResponseTerminalError(
                                    "response.completed", status="completed",
                                    reason="stream_final_mismatch",
                                )
                        usage = result.get("usage") if isinstance(result, dict) else None
                        if isinstance(usage, dict):
                            yield {"type": "usage", "usage": self._normalize_usage(usage)}
                        terminal = True
                        log_stream_metrics(terminal_kind="response.completed")
                        yield {"type": "finish", "reason": "completed", "stream_metrics": dict(stream_metrics)}
                        return
                    elif kind in {"response.incomplete", "response.failed"}:
                        result = event.get("response")
                        status = result.get("status") if isinstance(result, dict) else None
                        error = result.get("error") if isinstance(result, dict) else None
                        details = result.get("incomplete_details") if isinstance(result, dict) else None
                        reason = details.get("reason") if isinstance(details, dict) else None
                        error_code = error.get("code") if isinstance(error, dict) else None
                        log_stream_metrics(terminal_kind=str(kind))
                        raise DeepSeekResponseTerminalError(
                            str(kind), status=str(status) if status is not None else None,
                            reason=str(reason) if reason is not None else None,
                            error_code=str(error_code) if error_code is not None else None,
                            stream_metrics=stream_metrics,
                        )
            if not terminal:
                log_stream_metrics(terminal_kind="stream_disconnected")
                raise DeepSeekResponseTerminalError(
                    "stream_disconnected", reason="missing_terminal_event",
                    stream_metrics=stream_metrics,
                )
        except DeepSeekError:
            raise
        except Exception as exc:
            logger.exception("deepseek.responses_failed")
            kind = "timeout" if "timeout" in type(exc).__name__.casefold() else (
                "connect" if "connect" in type(exc).__name__.casefold() else "transport"
            )
            raise DeepSeekTransportError(kind, received_content=bool(streamed_parts)) from exc

    async def check(self) -> None:
        if not self.api_key:
            raise DeepSeekError(f"未配置 {self.api_key_env}")
        try:
            import httpx  # type: ignore
            response = await (await self._http_client()).get(
                f"{self.base_url}/models", headers=self._request_headers())
            if response.status_code >= 400:
                raise DeepSeekError(f"DeepSeek 鉴权检查失败：HTTP {response.status_code}")
            logger.info("deepseek.check_ok status=%d", response.status_code)
        except DeepSeekError:
            raise
        except Exception as exc:
            logger.exception("deepseek.check_failed")
            raise DeepSeekError(f"DeepSeek 鉴权检查失败：{exc}") from exc
