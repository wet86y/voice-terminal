from __future__ import annotations

from typing import Any

import httpx


class MiniMaxSearchError(RuntimeError):
    """A safe, user-facing error raised by the MiniMax search client."""

    disable_search = False
    transient = False
    service_status = "ERROR"


class MiniMaxSearchAuthError(MiniMaxSearchError):
    """The configured credential cannot use the configured MiniMax host."""

    disable_search = True
    service_status = "DISABLED"


class MiniMaxSearchTransientError(MiniMaxSearchError):
    """A timeout or temporary provider failure for this invocation only."""

    transient = True
    service_status = "READY"


class MiniMaxSearchClient:
    """Synchronous client for MiniMax Token Plan's web search endpoint.

    The client deliberately returns a small, normalized result rather than
    exposing the provider response directly to the voice model.
    """

    MAX_QUERY_CHARS = 256
    MAX_RESULTS = 2
    MAX_RELATED_SEARCHES = 0
    MAX_TITLE_CHARS = 160
    MAX_LINK_CHARS = 512
    MAX_SNIPPET_CHARS = 1200
    MAX_DATE_CHARS = 64
    MAX_RELATED_QUERY_CHARS = 256

    def __init__(self, *, base_url: str, api_key: str | None, timeout_ms: int = 5000) -> None:
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("MiniMax 搜索服务缺少 base_url")

        self.base_url = normalized_base_url
        self.api_key = str(api_key or "").strip()
        self.timeout_ms = max(1, int(timeout_ms))

    @classmethod
    def normalize_query(cls, query: Any) -> str:
        if not isinstance(query, str):
            raise ValueError("web_search.query 必须是字符串")
        normalized = query.strip()
        if not normalized:
            raise ValueError("web_search.query 不能为空")
        if len(normalized) > cls.MAX_QUERY_CHARS:
            raise ValueError(f"web_search.query 不能超过 {cls.MAX_QUERY_CHARS} 个字符")
        return normalized

    def search(self, query: str) -> dict[str, list[dict[str, str]]]:
        """Run a search and return bounded organic results and suggestions."""

        normalized_query = self.normalize_query(query)
        if not self.api_key:
            raise MiniMaxSearchAuthError("未配置 MiniMax 搜索 API key")

        try:
            timeout_seconds = self.timeout_ms / 1000
            timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # This is the non-sensitive source marker used by the
                # official MiniMax Token Plan MCP client.
                "MM-API-Source": "Minimax-MCP",
            }
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{self.base_url}/v1/coding_plan/search",
                    headers=headers,
                    json={"q": normalized_query},
                )
        except MiniMaxSearchError:
            raise
        except httpx.TimeoutException as exc:
            raise MiniMaxSearchTransientError("MiniMax 搜索请求超时") from exc
        except httpx.HTTPError as exc:
            raise MiniMaxSearchTransientError("MiniMax 搜索网络请求失败") from exc

        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError) as exc:
            raise MiniMaxSearchError("MiniMax 搜索返回了非法 HTTP 状态") from exc
        if status_code in {401, 403}:
            raise MiniMaxSearchAuthError("MiniMax 搜索鉴权失败，请确认 Token Plan key 与 host 匹配")
        if status_code >= 400:
            error_type = MiniMaxSearchTransientError if status_code in {408, 425, 429} or status_code >= 500 else MiniMaxSearchError
            raise error_type(f"MiniMax 搜索请求失败（HTTP {status_code}）")

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise MiniMaxSearchError("MiniMax 搜索返回了无效 JSON") from exc

        if not isinstance(payload, dict):
            raise MiniMaxSearchError("MiniMax 搜索返回格式非法")

        base_resp = payload.get("base_resp")
        if base_resp is not None and not isinstance(base_resp, dict):
            raise MiniMaxSearchError("MiniMax 搜索返回格式非法")
        if isinstance(base_resp, dict):
            provider_status = base_resp.get("status_code", 0)
            provider_ok = provider_status is None or provider_status == 0 or provider_status == "0"
            if not provider_ok:
                if provider_status in {1004, "1004"}:
                    raise MiniMaxSearchAuthError("MiniMax 搜索鉴权失败，请确认 Token Plan key 与 host 匹配")
                # Do not echo provider text: it is uncontrolled response data
                # and can contain credentials or a large/sensitive query.
                safe_code = str(provider_status) if isinstance(provider_status, int) else "nonzero"
                raise MiniMaxSearchError(f"MiniMax 搜索服务返回错误（code {safe_code}）")

        if "organic" not in payload:
            raise MiniMaxSearchError("MiniMax 搜索返回格式非法")
        organic_raw = payload["organic"]
        related_raw = payload.get("related_searches", [])
        if not isinstance(organic_raw, list) or not isinstance(related_raw, list):
            raise MiniMaxSearchError("MiniMax 搜索返回格式非法")

        organic: list[dict[str, str]] = []
        for item in organic_raw[: self.MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            title = self._safe_text(item.get("title"), self.MAX_TITLE_CHARS)
            link = self._safe_text(item.get("link") or item.get("url"), self.MAX_LINK_CHARS)
            snippet = self._safe_text(
                item.get("snippet") or item.get("content"),
                self.MAX_SNIPPET_CHARS,
            )
            date = self._safe_text(
                item.get("date") or item.get("page_age"),
                self.MAX_DATE_CHARS,
            )
            if not title and not link and not snippet:
                continue
            organic.append(
                {
                    "title": title,
                    "link": link,
                    "snippet": snippet,
                    "date": date,
                }
            )

        related_searches: list[dict[str, str]] = []
        for item in related_raw[: self.MAX_RELATED_SEARCHES]:
            if isinstance(item, dict):
                related_query = item.get("query") or item.get("title")
            else:
                related_query = item
            related_query = self._safe_text(related_query, self.MAX_RELATED_QUERY_CHARS)
            if related_query:
                related_searches.append({"query": related_query})

        return {"organic": organic, "related_searches": related_searches}

    @staticmethod
    def _safe_text(value: Any, limit: int) -> str:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return ""
        if not isinstance(value, (str, int, float, bool)):
            return ""
        return str(value).strip()[:limit]
