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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_ms = timeout_ms
        self.thinking = thinking
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, or max")
        self.reasoning_effort = reasoning_effort
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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        payload["thinking"] = {"type": "enabled" if self.thinking != "disabled" else "disabled"}
        if self.thinking != "disabled":
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

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
    ) -> AsyncIterator[dict]:
        if not self.api_key:
            raise DeepSeekError("未配置 DEEPSEEK_API_KEY")
        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise DeepSeekError("缺少 httpx，请先运行 scripts\\setup.ps1。") from exc

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
        payload = self._request_payload(messages)
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
