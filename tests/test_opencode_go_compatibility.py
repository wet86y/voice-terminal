from __future__ import annotations

import asyncio
import json

from voice_terminal.agent.decision_envelope import SpeechDecisionEnvelopeStream
from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.config import AppConfig, LlmConfig
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_provider_profiles_and_process_override(monkeypatch):
    default = AppConfig.from_mapping({}).llm
    assert default.provider == "deepseek"
    assert default.base_url == "https://api.deepseek.com"
    assert default.model == "deepseek-flash"
    assert default.api_key_env == "DEEPSEEK_API_KEY"

    opencode = AppConfig.from_mapping({"llm": {"provider": "opencode_go"}}).llm
    assert opencode.provider == "opencode_go"
    assert opencode.base_url == "https://opencode.ai/zen/go/v1"
    assert opencode.model == "deepseek-flash"
    assert opencode.api_key_env == "OPENCODE_GO_API_KEY"

    deepseek = AppConfig.from_mapping({"llm": {"provider": "deepseek"}}).llm
    assert deepseek.provider == "deepseek"
    assert deepseek.base_url == "https://api.deepseek.com"
    assert deepseek.model == "deepseek-flash"
    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"

    monkeypatch.setenv("VOICE_LLM_PROVIDER", "opencode_go")
    overridden = AppConfig.from_mapping({
        "llm": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
    }).llm
    assert overridden.provider == "opencode_go"
    assert overridden.base_url == "https://opencode.ai/zen/go/v1"
    assert overridden.model == "deepseek-flash"
    assert overridden.api_key_env == "OPENCODE_GO_API_KEY"


def test_llm_provider_rejects_unknown_profile():
    try:
        LlmConfig(provider="unknown")
    except ValueError as exc:
        assert "opencode_go" in str(exc)
    else:
        raise AssertionError("unknown LLM provider must be rejected")


def test_opencode_go_headers_are_session_bound_and_do_not_change_payload_protocol():
    client = DeepSeekClient(
        base_url="https://opencode.ai/zen/go/v1",
        model="deepseek-flash",
        api_key="secret-value",
        provider="opencode_go",
        api_key_env="OPENCODE_GO_API_KEY",
        session_id="conversation_a",
        transport="responses",
        thinking="disabled",
    )
    headers = client._request_headers(streaming=True)
    assert headers["Authorization"] == "Bearer secret-value"
    assert headers["User-Agent"] == "voice-terminal-backend/0.6"
    assert headers["x-opencode-session"] == "conversation_a"
    assert headers["Accept"] == "text/event-stream"
    assert client._responses_payload(
        [ContextFrame.make(ContextLane.HISTORY, "user", "hello")],
        model=client.model,
        schema=decision_schema([]),
        schema_name="voice_decision_v06",
        reasoning_effort="none",
        max_output_tokens=512,
    )["model"] == "deepseek-flash"

    client.set_session_id("conversation_b")
    assert client._request_headers()["x-opencode-session"] == "conversation_b"
    client.set_session_id(None)
    assert "x-opencode-session" not in client._request_headers()


def test_opencode_go_responses_sse_maps_to_existing_decision_envelope():
    async def scenario():
        full = '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'
        captured: dict = {}

        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: ' + json.dumps({"type": "response.created"})
                yield 'data: ' + json.dumps({
                    "type": "response.output_text.delta", "delta": full[:25],
                })
                yield 'data: ' + json.dumps({
                    "type": "response.output_text.delta", "delta": full[25:],
                })
                yield 'data: ' + json.dumps({
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 11, "output_tokens": 8},
                    },
                })

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Http:
            def stream(self, method, url, *, headers, json):
                captured.update(method=method, url=url, headers=headers, json=json)
                return Context()

        client = DeepSeekClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-flash",
            api_key="secret-value",
            provider="opencode_go",
            session_id="conversation_a",
            transport="responses",
            thinking="disabled",
        )
        client._client = Http()
        events = [item async for item in client.stream_events(
            [ContextFrame.make(ContextLane.HISTORY, "user", "hello")],
            schema=decision_schema([]),
        )]

        parser = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
        for item in events:
            if item["type"] == "content":
                parser.feed(item["text"])
        decision = parser.finish()

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/responses")
        assert captured["headers"]["x-opencode-session"] == "conversation_a"
        assert captured["headers"]["User-Agent"] == "voice-terminal-backend/0.6"
        assert captured["json"]["model"] == "deepseek-flash"
        assert captured["json"]["text"]["format"]["strict"] is True
        assert decision.speak.state == "wait"
        assert decision.speak.text == ""
        assert any(item["type"] == "usage" for item in events)
        assert events[-1]["type"] == "finish"
        assert events[-1]["reason"] == "completed"
        assert events[-1]["stream_metrics"]["sse_event_count"] == 4
        assert "secret-value" not in json.dumps(captured["json"], ensure_ascii=False)

    asyncio.run(scenario())


def test_session_reset_rotates_opencode_session_identity():
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime._session_lock = asyncio.Lock()
        runtime.controller = None
        runtime.history = SessionLedger()
        runtime.conversation = ConversationRuntime()
        runtime.reasoning_mode = "deep"
        runtime.llm = DeepSeekClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-flash",
            api_key="secret-value",
            provider="opencode_go",
            session_id=runtime.conversation.conversation_id,
        )
        before = runtime.conversation.conversation_id
        assert runtime.llm.session_id == before

        await runtime.reset_session()

        assert runtime.conversation.conversation_id != before
        assert runtime.llm.session_id == runtime.conversation.conversation_id
        assert runtime.reasoning_mode == "deep"

    asyncio.run(scenario())
