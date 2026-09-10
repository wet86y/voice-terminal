from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout_ms: int = 15000,
        thinking: str = "disabled",
        reasoning_effort: str = "low",
        transport: str = "chat_completions",
        max_output_tokens: int = 512,
        shadow_enabled: bool = False,
        shadow_sample_rate: float = 1.0,
        shadow_thinking_enabled: bool = False,
        thinking_action_enabled: bool = False,
        protocol_recovery_limit: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_ms = timeout_ms
        self.thinking = thinking
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, or max")
        self.reasoning_effort = reasoning_effort
        if transport not in {"chat_completions", "responses"}:
            raise ValueError("transport must be chat_completions or responses")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not 0 <= shadow_sample_rate <= 1:
            raise ValueError("shadow_sample_rate must be between zero and one")
        self.transport = transport
        self.max_output_tokens = max_output_tokens
        self.shadow_enabled = shadow_enabled
        self.shadow_sample_rate = shadow_sample_rate
        self.shadow_thinking_enabled = shadow_thinking_enabled
        self.thinking_action_enabled = thinking_action_enabled
        self.protocol_recovery_limit = max(0, protocol_recovery_limit)
        self.supports_decision_schema = True
        self._client = None

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

    async def prewarm(self) -> None:
        """Warm DNS/TCP/TLS/auth transport only; never starts inference."""
        if not self.api_key:
            return
        client = await self._http_client()
        response = await client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code >= 400:
            raise DeepSeekError(f"DeepSeek 预热失败：HTTP {response.status_code}")
        logger.info("deepseek.prewarm_ok status=%d", response.status_code)

    def _request_payload(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        effective_effort = reasoning_effort or self.reasoning_effort
        thinking_enabled = reasoning_effort is not None or self.thinking != "disabled"
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled:
            payload["reasoning_effort"] = effective_effort
        return payload

    @staticmethod
    def _responses_payload(
        messages: Sequence[Mapping[str, str]], *, model: str, schema: Mapping[str, Any],
        schema_name: str, reasoning_effort: str, max_output_tokens: int,
    ) -> dict[str, Any]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
            if role == "system":
                instructions.append(content)
                continue
            input_items.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": [{"type": "input_text", "text": content}],
            })
        compatibility = (
            "Provider schema compatibility: every declared property is required. "
            "For wait/cancel set speak.text to the empty string. "
            "For action arguments, set fields irrelevant to that action to empty/neutral values; "
            "the local runtime will retain only that action's declared fields."
        )
        return {
            "model": model,
            "instructions": "\n\n".join([*instructions, compatibility]),
            "input": input_items,
            "reasoning": {"effort": reasoning_effort},
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
        messages: Sequence[Mapping[str, str]],
        *,
        cancel_event: Any = None,
        json_mode: bool = False,
        transport: str | None = None,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "voice_decision_v06",
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict]:
        if not self.api_key:
            raise DeepSeekError("未配置 DEEPSEEK_API_KEY")
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise DeepSeekError("缺少 httpx，请先运行 scripts\\setup.ps1。") from exc

        selected_transport = transport or self.transport
        if selected_transport == "responses":
            if schema is None:
                raise DeepSeekError("Responses transport requires a decision JSON Schema")
            async for item in self._stream_responses(
                messages, schema=schema, schema_name=schema_name,
                reasoning_effort=reasoning_effort or (self.reasoning_effort if self.thinking != "disabled" else "none"),
                cancel_event=cancel_event,
            ):
                yield item
            return
        if selected_transport != "chat_completions":
            raise DeepSeekError(f"unsupported DeepSeek transport: {selected_transport}")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
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
                    logger.error("deepseek.http_error status=%d body=%r", response.status_code, body[:500])
                    raise DeepSeekError(f"DeepSeek HTTP {response.status_code}: {body[:500]}")
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
                            yield {"type": "usage", "usage": usage}
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
            raise DeepSeekError(f"DeepSeek 请求失败：{exc}") from exc

    async def _stream_responses(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        reasoning_effort: str,
        cancel_event: Any = None,
    ) -> AsyncIterator[dict]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = self._responses_payload(
            messages, model=self.model, schema=schema, schema_name=schema_name,
            reasoning_effort=reasoning_effort, max_output_tokens=self.max_output_tokens,
        )
        terminal = False
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
                    raise DeepSeekError(f"DeepSeek Responses HTTP {response.status_code}: {body[:500]}")
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
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield {"type": "content", "text": delta}
                    elif kind == "response.reasoning_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield {"type": "reasoning", "text": delta}
                    elif kind == "response.completed":
                        result = event.get("response")
                        status = result.get("status") if isinstance(result, dict) else None
                        if status not in {None, "completed"}:
                            raise DeepSeekError(f"Responses completed with invalid status: {status}")
                        usage = result.get("usage") if isinstance(result, dict) else None
                        if isinstance(usage, dict):
                            normalized = dict(usage)
                            if isinstance(usage.get("input_tokens"), int):
                                normalized["prompt_tokens"] = usage["input_tokens"]
                            if isinstance(usage.get("output_tokens"), int):
                                normalized["completion_tokens"] = usage["output_tokens"]
                            yield {"type": "usage", "usage": normalized}
                        terminal = True
                        yield {"type": "finish", "reason": "completed"}
                        return
                    elif kind in {"response.incomplete", "response.failed"}:
                        result = event.get("response")
                        status = result.get("status") if isinstance(result, dict) else None
                        error = result.get("error") if isinstance(result, dict) else None
                        raise DeepSeekError(f"Responses terminal failure: {kind}/{status}: {str(error)[:300]}")
            if not terminal:
                raise DeepSeekError("Responses stream disconnected before response.completed")
        except DeepSeekError:
            raise
        except Exception as exc:
            logger.exception("deepseek.responses_failed")
            raise DeepSeekError(f"DeepSeek Responses 请求失败：{exc}") from exc

    async def check(self) -> None:
        if not self.api_key:
            raise DeepSeekError("未配置 DEEPSEEK_API_KEY")
        try:
            import httpx  # type: ignore
            response = await (await self._http_client()).get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            if response.status_code >= 400:
                raise DeepSeekError(f"DeepSeek 鉴权检查失败：HTTP {response.status_code}")
            logger.info("deepseek.check_ok status=%d", response.status_code)
        except DeepSeekError:
            raise
        except Exception as exc:
            logger.exception("deepseek.check_failed")
            raise DeepSeekError(f"DeepSeek 鉴权检查失败：{exc}") from exc
