from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from voice_terminal.agent.speculative_tools import SpeculativeTools
from voice_terminal.agent.tool_registry import FileTools, WebSearchTools
from voice_terminal.agent.runtime import AgentRuntime
from voice_terminal.providers.minimax_search import (
    MiniMaxSearchAuthError,
    MiniMaxSearchClient,
    MiniMaxSearchError,
    MiniMaxSearchTransientError,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeHttpClient:
    response = FakeResponse(200, {"organic": [], "related_searches": []})
    calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class MiniMaxSearchClientTests(unittest.TestCase):
    def setUp(self):
        FakeHttpClient.calls = []
        FakeHttpClient.response = FakeResponse(200, {"organic": [], "related_searches": []})
        self.client = MiniMaxSearchClient(
            base_url="https://api.minimaxi.com/",
            api_key="test-token-that-must-not-leak",
        )

    def test_posts_q_with_bearer_and_source_header_and_normalizes_results(self):
        FakeHttpClient.response = FakeResponse(200, {
            "base_resp": {"status_code": 0},
            "organic": [{"title": "Title", "link": "https://example.test", "snippet": "Summary", "date": "2026-09-06"}],
            "related_searches": [],
        })

        with patch("httpx.Client", return_value=FakeHttpClient()):
            result = self.client.search("  current voice agents  ")

        self.assertEqual(result, {
            "organic": [{
                "title": "Title",
                "link": "https://example.test",
                "snippet": "Summary",
                "date": "2026-09-06",
            }],
            "related_searches": [],
        })
        url, kwargs = FakeHttpClient.calls[0]
        self.assertEqual(url, "https://api.minimaxi.com/v1/coding_plan/search")
        self.assertEqual(kwargs["json"], {"q": "current voice agents"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token-that-must-not-leak")
        self.assertEqual(kwargs["headers"]["MM-API-Source"], "Minimax-MCP")

    def test_bounds_normalized_result_fields_and_counts(self):
        FakeHttpClient.response = FakeResponse(200, {
            "organic": [{
                "title": "t" * 500,
                "link": "l" * 5000,
                "snippet": "s" * 5000,
                "date": "d" * 500,
            } for _ in range(20)],
            "related_searches": [{"query": "q" * 500} for _ in range(20)],
        })

        with patch("httpx.Client", return_value=FakeHttpClient()):
            result = self.client.search("bounded")

        self.assertEqual(len(result["organic"]), 2)
        self.assertEqual(result["related_searches"], [])
        self.assertEqual(len(result["organic"][0]["title"]), 160)
        self.assertEqual(len(result["organic"][0]["link"]), 512)
        self.assertEqual(len(result["organic"][0]["snippet"]), 1200)
        self.assertEqual(len(result["organic"][0]["date"]), 64)

    def test_invalid_query_and_response_are_rejected(self):
        with self.assertRaises(ValueError):
            self.client.search(" ")
        with self.assertRaises(ValueError):
            self.client.search("x" * 257)

        for payload in (None, {}, {"organic": "not-a-list", "related_searches": []}, {"organic": [], "related_searches": {}}):
            FakeHttpClient.response = FakeResponse(200, payload)
            with patch("httpx.Client", return_value=FakeHttpClient()), self.assertRaises(MiniMaxSearchError):
                self.client.search("bad response")

    def test_provider_errors_are_safe_and_auth_errors_are_distinct(self):
        secret = self.client.api_key
        cases = [
            (FakeResponse(401, {"secret": secret}), MiniMaxSearchAuthError),
            (FakeResponse(500, {"secret": secret}), MiniMaxSearchError),
            (FakeResponse(200, {"base_resp": {"status_code": 1004, "status_msg": secret}, "organic": [], "related_searches": []}), MiniMaxSearchAuthError),
            (FakeResponse(200, {"base_resp": {"status_code": 9, "status_msg": secret}, "organic": [], "related_searches": []}), MiniMaxSearchError),
            (FakeResponse(200, ValueError("invalid json")), MiniMaxSearchError),
        ]
        for response, error_type in cases:
            FakeHttpClient.response = response
            with patch("httpx.Client", return_value=FakeHttpClient()):
                with self.assertRaises(error_type) as raised:
                    self.client.search("safe error")
            self.assertNotIn(secret, str(raised.exception))

        FakeHttpClient.response = httpx.ReadTimeout("network timeout")
        with patch("httpx.Client", return_value=FakeHttpClient()):
            with self.assertRaises(MiniMaxSearchTransientError) as raised:
                self.client.search("timeout")
        self.assertNotIn(secret, str(raised.exception))

    def test_missing_key_is_an_auth_error_without_echoing_key(self):
        client = MiniMaxSearchClient(base_url="https://api.minimaxi.com", api_key=None)
        with self.assertRaises(MiniMaxSearchAuthError) as raised:
            client.search("missing key")
        self.assertNotIn("None", str(raised.exception))
        self.assertEqual(WebSearchTools(client).schemas(), [])


class WebSearchToolsTests(unittest.TestCase):
    class Client:
        normalize_query = staticmethod(MiniMaxSearchClient.normalize_query)

        def __init__(self):
            self.calls = 0

        def search(self, query):
            self.calls += 1
            return {"organic": [{"title": query, "link": "", "snippet": "", "date": ""}], "related_searches": []}

    def test_schema_and_exact_argument_validation(self):
        tools = WebSearchTools(self.Client())
        self.assertEqual(tools.schemas()[0]["function"]["name"], "web_search")
        self.assertEqual(tools.normalize("web_search", {"query": " now "}), ("web_search", {"query": "now"}))
        with self.assertRaises(ValueError):
            tools.normalize("web_search", {"query": "now", "extra": "no"})
        tools.disable("auth mismatch")
        self.assertEqual(tools.schemas(), [])

    def test_auth_failure_disables_only_the_search_backend(self):
        class AuthClient(self.Client):
            def search(self, query):
                raise MiniMaxSearchAuthError("auth mismatch")

        tools = WebSearchTools(AuthClient())
        with self.assertRaises(MiniMaxSearchAuthError):
            tools.execute("web_search", {"query": "latest"})
        self.assertFalse(tools.available)
        self.assertEqual(tools.schemas(), [])

    def test_malformed_provider_response_is_error_but_transient_failure_stays_ready(self):
        malformed = self.Client()
        malformed.search = lambda query: {"organic": "bad", "related_searches": []}
        tools = WebSearchTools(malformed)
        with self.assertRaises(MiniMaxSearchError):
            tools.execute("web_search", {"query": "latest"})
        self.assertEqual(tools.service_status, "ERROR")
        self.assertEqual(tools.schemas(), [])

        transient_client = MiniMaxSearchClient(
            base_url="https://api.minimaxi.com",
            api_key="transient-test-key",
        )
        transient_tools = WebSearchTools(transient_client)
        FakeHttpClient.response = FakeResponse(503, {})
        with patch("httpx.Client", return_value=FakeHttpClient()):
            with self.assertRaises(MiniMaxSearchTransientError):
                transient_tools.execute("web_search", {"query": "latest"})
        self.assertTrue(transient_tools.available)
        self.assertEqual(transient_tools.service_status, "READY")

    def test_speculative_search_is_reused_for_same_turn_and_query(self):
        async def run():
            client = self.Client()
            registry = SpeculativeTools(FileTools(()), search=WebSearchTools(client))
            first, reused = await registry.select("turn-1", "web_search", {"query": "latest"})
            self.assertFalse(reused)
            self.assertEqual(await first.task, {
                "organic": [{"title": "latest", "link": "", "snippet": "", "date": ""}],
                "related_searches": [],
            })
            second, reused = await registry.select("turn-1", "web_search", {"query": " latest "})
            self.assertTrue(reused)
            self.assertIs(first, second)
            self.assertEqual(client.calls, 1)
            registry.cancel_turn("turn-1")
            registry.begin_turn("turn-2")
            third, reused = await registry.select("turn-2", "web_search", {"query": "latest"})
            self.assertFalse(reused)
            await third.task
            self.assertEqual(client.calls, 2)
            registry.close()

        asyncio.run(run())


class AgentRuntimeSearchTests(unittest.TestCase):
    class SearchClient:
        normalize_query = staticmethod(MiniMaxSearchClient.normalize_query)

        def __init__(self):
            self.calls = 0

        def search(self, query):
            self.calls += 1
            return {
                "organic": [{
                    "title": "MiniMax result",
                    "link": "https://example.test",
                    "snippet": f"summary for {query}",
                    "date": "2026-09-06",
                }],
                "related_searches": [],
            }

    def test_prediction_and_final_answer_receive_search_schema_and_tool_result(self):
        class PredictionProvider:
            def __init__(self):
                self.messages = None

            async def stream_events(self, messages, **kwargs):
                self.messages = messages
                yield {"type": "content", "text": '{"decision":"ACT","intent":"查实时信息","tools":[{"name":"web_search","arguments":{"query":"latest"}}]}'}

        prediction_provider = PredictionProvider()
        prediction_client = self.SearchClient()
        prediction_registry = SpeculativeTools(FileTools(()), search=WebSearchTools(prediction_client))
        try:
            prediction = asyncio.run(AgentRuntime(
                prediction_provider, prediction_registry, tools_enabled=True
            ).predict([], {"turn_id": "turn", "generation_id": 0, "input_revision": 1, "text": "latest"}))
            self.assertEqual(prediction["tools"][0]["name"], "web_search")
            self.assertIn("web_search", prediction_provider.messages[0]["content"])
        finally:
            prediction_registry.close()

        class AnswerProvider:
            def __init__(self):
                self.requests = []

            async def stream_events(self, messages, **kwargs):
                self.requests.append((messages, kwargs))
                if len(self.requests) == 1:
                    yield {"type": "tool_delta", "call": {
                        "index": 0,
                        "id": "call-web",
                        "function": {"name": "web_search", "arguments": '{"query":"latest"}'},
                    }}
                    yield {"type": "finish", "reason": "tool_calls"}
                else:
                    self.assert_tool_result(messages)
                    yield {"type": "content", "text": "根据最新摘要，当前信息已更新。"}
                    yield {"type": "finish", "reason": "stop"}

            @staticmethod
            def assert_tool_result(messages):
                tool_messages = [item for item in messages if item.get("role") == "tool"]
                if not tool_messages or "MiniMax result" not in tool_messages[-1]["content"]:
                    raise AssertionError("search result was not returned to the LLM")

        answer_provider = AnswerProvider()
        answer_client = self.SearchClient()
        answer_registry = SpeculativeTools(FileTools(()), search=WebSearchTools(answer_client))
        events = []

        async def run_answer():
            async def on_tool(payload):
                events.append(payload)

            return [piece async for piece in AgentRuntime(
                answer_provider, answer_registry, tools_enabled=True
            ).answer([], {"final_text": "latest", "final": True}, "turn", on_tool)]

        try:
            pieces = asyncio.run(run_answer())
        finally:
            answer_registry.close()
        self.assertEqual(answer_client.calls, 1)
        self.assertEqual([event["status"] for event in events], ["RUNNING", "DONE"])
        self.assertIn("web_search", {
            schema["function"]["name"] for schema in answer_provider.requests[0][1]["tools"]
        })
        self.assertIn("根据最新摘要", "".join(piece for piece in pieces if piece))


if __name__ == "__main__":
    unittest.main()
