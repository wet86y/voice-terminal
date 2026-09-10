from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.decision_envelope import SpeechDecisionEnvelopeStream
from voice_terminal.agent.decision_schema import THINKING_ACTION, decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.providers.deepseek import DeepSeekError
from voice_terminal.providers.deepseek import DeepSeekResponseTerminalError
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_decision_schema_keeps_wire_shape_and_reserves_thinking_action():
    schema = decision_schema([{
        "name": "web_search",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }], allow_thinking=True)
    assert schema["required"] == ["speak", "actions"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["actions"]["items"]
    assert item["required"] == ["name", "arguments"]
    assert item["properties"]["name"]["enum"] == ["web_search", THINKING_ACTION]
    assert item["properties"]["arguments"]["properties"]["effort"] == {"type": "string"}


def test_responses_control_text_compatibility_is_explicit_not_global():
    raw = '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'
    strict_chat = SpeechDecisionEnvelopeStream()
    strict_chat.feed(raw)
    try:
        strict_chat.finish()
    except ValueError as exc:
        assert "cannot contain text" in str(exc)
    else:
        raise AssertionError("the frozen Chat contract must still reject wait.text")

    responses = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
    responses.feed(raw)
    assert responses.finish().speak == SpeechDecision("wait", "calm", "")


def test_responses_payload_separates_instructions_and_enables_strict_schema():
    client = DeepSeekClient(
        base_url="https://example.invalid", model="deepseek-v4-flash", api_key="key",
        transport="responses", thinking="disabled",
    )
    schema = decision_schema([], allow_thinking=False)
    payload = client._responses_payload(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}],
        model=client.model, schema=schema, schema_name="voice_decision_v06",
        reasoning_effort="none", max_output_tokens=512,
    )
    assert payload["instructions"].startswith("rules\n\n")
    assert "wait/cancel set speak.text to the empty string" in payload["instructions"]
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"] == schema
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": "hello"}


def test_responses_stream_normalizes_content_usage_and_terminal():
    async def scenario():
        lines = [
            'data: {"type":"response.created"}',
            'data: {"type":"response.output_text.delta","delta":"{\\"speak\\""}',
            'data: {"type":"response.output_text.delta","delta":":{\\"state\\":\\"wait\\"},\\"actions\\":[]}"}',
            'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":12,"output_tokens":7}}}',
        ]

        class Response:
            status_code = 200

            async def aiter_lines(self):
                for line in lines:
                    yield line

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Http:
            def stream(self, *_args, **_kwargs):
                return Context()

        client = DeepSeekClient(
            base_url="https://example.invalid", model="model", api_key="key",
            transport="responses", thinking="disabled",
        )
        client._client = Http()
        events = [item async for item in client.stream_events(
            [{"role": "user", "content": "hello"}], schema=decision_schema([]),
        )]
        assert [item["type"] for item in events] == ["content", "content", "usage", "finish"]
        assert events[-2]["usage"]["prompt_tokens"] == 12
        assert events[-2]["usage"]["completion_tokens"] == 7
        assert events[-1] == {"type": "finish", "reason": "completed"}

    asyncio.run(scenario())


def test_responses_failed_terminal_never_yields_finish():
    async def scenario():
        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"bad"}}}'

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Http:
            def stream(self, *_args, **_kwargs):
                return Context()

        client = DeepSeekClient(
            base_url="https://example.invalid", model="model", api_key="key", transport="responses",
        )
        client._client = Http()
        try:
            _ = [item async for item in client.stream_events(
                [{"role": "user", "content": "hello"}], schema=decision_schema([]),
            )]
        except DeepSeekError as exc:
            assert "response.failed" in str(exc)
        else:
            raise AssertionError("failed Responses terminal must raise")

    asyncio.run(scenario())


def test_responses_done_text_repairs_missing_delta_suffix_before_finish():
    async def scenario():
        full = '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'
        prefix = full[:24]

        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: ' + json.dumps({"type": "response.output_text.delta", "delta": prefix})
                yield 'data: ' + json.dumps({"type": "response.output_text.done", "text": full})
                yield 'data: {"type":"response.completed","response":{"status":"completed"}}'

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Http:
            def stream(self, *_args, **_kwargs):
                return Context()

        client = DeepSeekClient(
            base_url="https://example.invalid", model="model", api_key="key",
            transport="responses", thinking="disabled",
        )
        client._client = Http()
        events = [item async for item in client.stream_events(
            [{"role": "user", "content": "hello"}], schema=decision_schema([]),
        )]
        assert "".join(item["text"] for item in events if item["type"] == "content") == full
        assert events[-1] == {"type": "finish", "reason": "completed"}

    asyncio.run(scenario())


def test_responses_incomplete_preserves_machine_readable_reason():
    async def scenario():
        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield ('data: {"type":"response.incomplete","response":{"status":"incomplete",'
                       '"incomplete_details":{"reason":"max_output_tokens"}}}')

        class Context:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Http:
            def stream(self, *_args, **_kwargs):
                return Context()

        client = DeepSeekClient(
            base_url="https://example.invalid", model="model", api_key="key",
            transport="responses", thinking="disabled",
        )
        client._client = Http()
        try:
            _ = [item async for item in client.stream_events(
                [{"role": "user", "content": "hello"}], schema=decision_schema([]),
            )]
        except DeepSeekResponseTerminalError as exc:
            assert exc.terminal_kind == "response.incomplete"
            assert exc.reason == "max_output_tokens"
        else:
            raise AssertionError("incomplete terminal must remain distinguishable")

    asyncio.run(scenario())


def test_runtime_gives_max_output_incomplete_the_single_protocol_recovery():
    async def scenario():
        class Provider:
            supports_decision_schema = True
            transport = "responses"
            thinking_action_enabled = False
            protocol_recovery_limit = 1

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    raise DeepSeekResponseTerminalError(
                        "response.incomplete", status="incomplete",
                        reason="max_output_tokens",
                    )
                    yield  # pragma: no cover

                return stream()

        runtime = _runtime(Provider())
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-a")
        runtime._identity_turns[identity] = "turn-a"
        # Exercise _step in isolation; the scheduler behavior is covered by
        # test_whitespace_recovery_reproduction.py.
        runtime.runner = asyncio.create_task(asyncio.Event().wait())
        outcome = await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert outcome == "recovery_queued"
        assert len(runtime.recovery_queue) == 1
        assert runtime.recovery_queue[0].code == "RESPONSES_INCOMPLETE"
        runtime.runner.cancel()
        await asyncio.gather(runtime.runner, return_exceptions=True)

    asyncio.run(scenario())


def test_runtime_does_not_ask_model_to_correct_content_filter():
    async def scenario():
        class Provider:
            supports_decision_schema = True
            transport = "responses"
            thinking_action_enabled = False
            protocol_recovery_limit = 1

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    raise DeepSeekResponseTerminalError(
                        "response.incomplete", status="incomplete",
                        reason="content_filter",
                    )
                    yield  # pragma: no cover

                return stream()

        runtime = _runtime(Provider())
        emitted = []

        async def emit(kind, payload):
            emitted.append((kind, payload))

        runtime.emit = emit
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-a")
        runtime._identity_turns[identity] = "turn-a"
        outcome = await runtime._step(identity, {"USER_INPUT_FINALIZED"})
        assert outcome == "terminal_failure"
        assert not runtime.recovery_queue
        assert any(kind == "error" and payload.get("code") == "LLM_OUTPUT_FILTERED"
                   for kind, payload in emitted)

    asyncio.run(scenario())


def test_transport_retry_exhaustion_waits_for_an_external_event():
    async def scenario():
        class Provider:
            supports_decision_schema = False
            transport = "responses"
            transport_retry_limit = 0

            def __init__(self):
                self.calls = 0

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    self.calls += 1
                    raise DeepSeekError("DeepSeek Responses HTTP 503: unavailable")
                    yield  # pragma: no cover

                return stream()

        provider = Provider()
        runtime = _runtime(provider)
        emitted = []
        unavailable = asyncio.Event()

        async def emit(kind, payload):
            emitted.append((kind, payload))
            if kind == "error" and payload.get("code") == "LLM_PROVIDER_UNAVAILABLE":
                unavailable.set()

        runtime.emit = emit
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        runtime._wake("USER_INPUT_FINALIZED")
        await asyncio.wait_for(unavailable.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert provider.calls == 1
        assert not runtime.pending and not runtime.recovery_queue
        assert any(kind == "agent.turn_settled" and payload.get("outcome") == "provider_unavailable"
                   for kind, payload in emitted)
        runtime.runner.cancel()
        await asyncio.gather(runtime.runner, return_exceptions=True)

    asyncio.run(scenario())


class _Registry:
    def begin_turn(self, _turn):
        pass

    def cancel_turn(self, _turn):
        pass

    def action_definitions(self):
        return []


class _Output:
    status = "IDLE"
    active = False
    speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

    def cancel_all(self, _reason):
        pass


def _runtime(provider):
    async def emit(*_args):
        pass

    conversation = ConversationRuntime()
    conversation.begin_turn("turn-a")
    runtime = AgentEpisodeRuntime(
        provider, _Registry(), _Output(), emit,
        conversation=conversation, ledger=SessionLedger(),
    )
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    return runtime


def test_shadow_chain_validates_without_touching_output_or_ledger():
    async def scenario():
        class Provider:
            supports_decision_schema = True
            shadow_enabled = True
            shadow_sample_rate = 1.0
            shadow_thinking_enabled = False
            transport = "chat_completions"

            def stream_events(self, _messages, **kwargs):
                assert kwargs["transport"] == "responses"
                assert kwargs["reasoning_effort"] == "none"

                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"wait"},"actions":[]}'}
                    yield {"type": "usage", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                    yield {"type": "finish", "reason": "completed"}

                return stream()

        runtime = _runtime(Provider())
        before = list(runtime.ledger.records)
        await runtime._run_shadow_chain(({"role": "user", "content": "hello"},), "turn-a")
        assert list(runtime.ledger.records) == before
        assert runtime.output.status == "IDLE"
        assert runtime.shadow_results[-1]["valid"] is True
        assert runtime.shadow_results[-1]["state"] == "wait"
        assert runtime.shadow_results[-1]["prompt_tokens"] == 10

    asyncio.run(scenario())


def test_thinking_and_business_actions_release_one_barrier_after_tools():
    async def scenario():
        provider = SimpleNamespace(thinking_action_enabled=True)
        runtime = _runtime(provider)
        released = asyncio.Event()
        wakes = []

        async def execute(_identity, _turn_id, actions, *, wake=True):
            assert [action.name for action in actions] == ["web_search"]
            assert wake is False
            await released.wait()
            return True

        runtime._execute_actions_v06 = execute
        runtime._wake = wakes.append
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "让我想一想。"),
            (
                Action(THINKING_ACTION, {"effort": "low"}),
                Action("web_search", {"query": "current weather"}),
            ),
        )
        await runtime._dispatch_actions_v06((runtime.episode_id, "request"), "turn-a", envelope)
        await asyncio.sleep(0)
        assert wakes == [] and runtime._thinking_lease_turn is None
        released.set()
        await asyncio.gather(*tuple(runtime._background_tools))
        assert wakes == ["THINKING_READY"]
        assert runtime._thinking_lease_turn == "turn-a"
        assert runtime._thinking_prefix_by_turn["turn-a"] == "让我想一想。"

    asyncio.run(scenario())


def test_new_user_turn_invalidates_pending_thinking_barrier():
    async def scenario():
        provider = SimpleNamespace(thinking_action_enabled=True)
        runtime = _runtime(provider)
        runtime._wake = lambda _kind: None
        runtime._thinking_lease_turn = "turn-a"
        runtime._thinking_prefix_by_turn["turn-a"] = "让我想想。"
        generation = runtime._thinking_barrier_generation
        await runtime.observe("USER_SEGMENT_UPDATED", turn_id="turn-b", text="new input")
        assert runtime.last_turn_id == "turn-b"
        assert runtime._thinking_lease_turn is None
        assert "turn-a" not in runtime._thinking_prefix_by_turn
        assert runtime._thinking_barrier_generation == generation + 1

    asyncio.run(scenario())
