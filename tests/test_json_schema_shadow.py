from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.context_audit import build_context_snapshot
from voice_terminal.agent.decision_envelope import (
    EnvelopeError, SpeechDecisionEnvelopeStream, envelope_error_diagnostics,
    json_structure_fingerprint, parse_compatible_decision_envelope,
    provider_schema_diagnostics,
)
from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.agent.episode import AgentEpisodeRuntime, PROMPT
from voice_terminal.providers.deepseek import DeepSeekClient, DeepSeekHttpError, DeepSeekTransportError
from voice_terminal.providers.deepseek import DeepSeekError
from voice_terminal.providers.deepseek import DeepSeekResponseTerminalError
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.shadow_agent.contracts import ShadowEvent


def history(role: str, content: str) -> dict[str, str]:
    return ContextFrame.make(ContextLane.HISTORY, role, content)


def request_tail_by_kind(messages):
    """按 context_kind（上下文类型）读取独立动态尾部帧。"""
    return {
        item["context_kind"]: item["content"]
        for item in messages
        if item.get("context_lane") == ContextLane.REQUEST_TAIL
        and item.get("context_kind")
    }


def static(content: str) -> dict[str, str]:
    return ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", content)


def test_decision_schema_keeps_wire_shape_without_extra_control_actions():
    schema = decision_schema([{
        "name": "web_search",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }])
    assert schema["required"] == ["speak", "actions"]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["actions"]["items"]
    assert item["required"] == ["name", "arguments"]
    assert item["properties"]["name"]["enum"] == ["web_search"]
    assert item["properties"]["arguments"]["properties"] == {"query": {"type": "string"}}


def test_prompt_matches_required_responses_wire_contract_and_has_short_examples():
    assert "state、emotion、text 以及顶层 actions 始终必填" in PROMPT
    assert "state=wait 时 text 必须为 \"\"" in PROMPT
    assert "state=cancel 时 text 为空表示只停止，text 非空表示停止并替换" in PROMPT
    examples = re.findall(r'\{"speak":\{[^{}\n]+\},"actions":\[\]\}', PROMPT)
    states = set()
    for example in examples:
        parser = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
        parser.feed(example)
        states.add(parser.finish().speak.state)
    # Revision（尾巴修订）仍受协议允许，但不再用静态示例诱发重播。
    assert states == {"speak", "wait"}
    assert "state 只能是 speak、revision、cancel、wait" in PROMPT
    assert "emotion 可省略" not in PROMPT
    assert "不得带 text" not in PROMPT


def test_shadow_information_and_terminal_wake_main_scheduler_but_progress_does_not():
    async def scenario():
        class Registry:
            def action_definitions(self):
                return []

            def begin_turn(self, _episode_id):
                return None

        async def emit(*_args):
            return None

        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(status="IDLE"), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.ledger.set_shadow_runtime_state(available=True, active=True)
        wakes = []
        runtime._wake = wakes.append
        await runtime.accept_shadow_event(
            ShadowEvent("p", "progress", "正在检索", 1)
        )
        await runtime.accept_shadow_event(
            ShadowEvent("s", "information", "正在等待用户说明", 1)
        )
        model_context = runtime.messages({"SHADOW_INFORMATION"},
                                         shadow_information=runtime.ledger.shadow_information_snapshot())
        assert any("正在等待用户说明" in item["content"] for item in model_context)
        await runtime.accept_shadow_event(
            ShadowEvent("q", "input_required", "请选择范围", 1,
                        options=("A", "B"))
        )
        assert wakes == ["SHADOW_INFORMATION", "SHADOW_EVENT_PRIORITY"]
        payload = runtime.ledger.entries[-1].content
        assert payload["kind"] == "input_required"
        assert payload["options"] == ["A", "B"]
        assert runtime.ledger.shadow_information_snapshot() is None

    asyncio.run(scenario())


def test_provider_schema_diagnostics_are_separate_from_local_contract():
    schema = decision_schema([])
    missing_emotion = '{"speak":{"state":"wait","text":""},"actions":[]}'
    result = provider_schema_diagnostics(missing_emotion, schema)
    assert result == {
        "transport_valid": True,
        "json_syntax_valid": True,
        "provider_schema_valid": False,
        "provider_schema_error_code": "MISSING_REQUIRED",
        "provider_schema_error_path": "$.speak.emotion",
    }

    error = EnvelopeError("incomplete or invalid decision JSON")
    prose = envelope_error_diagnostics("普通正文", error, schema)
    assert prose["error_layer"] == "json_syntax"
    assert prose["error_code"] == "PROSE_INSTEAD_OF_JSON"
    assert prose["content_class"] == "prose"


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


def test_stream_parser_detects_only_definitive_non_json_prefixes_after_ten_chars():
    prose = SpeechDecisionEnvelopeStream()
    prose.feed("这是普通文本回复")  # 8 effective characters
    assert prose.definitive_non_json_prefix() is False
    prose.feed("而不是")  # 11 effective characters
    assert prose.definitive_non_json_prefix() is True

    spaced = SpeechDecisionEnvelopeStream()
    spaced.feed("  普 通 文 本 回 复 测 试 ")
    assert spaced.definitive_non_json_prefix() is False
    spaced.feed("错误")
    assert spaced.definitive_non_json_prefix() is True

    for raw in (
        '   {"speak":',
        '\ufeff  {"speak":',
        '```json\n  {"speak":',
    ):
        parser = SpeechDecisionEnvelopeStream()
        parser.feed(raw)
        assert parser.definitive_non_json_prefix() is False

    fenced_prose = SpeechDecisionEnvelopeStream()
    fenced_prose.feed("```json\n这不是合法的结构化输出文本")
    assert fenced_prose.definitive_non_json_prefix() is True


def test_runtime_aborts_prose_stream_at_ten_effective_chars_and_starts_recovery():
    async def scenario():
        class Provider:
            supports_decision_schema = True
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.generated = 0
                self.closed = asyncio.Event()

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    try:
                        for char in "这是模型输出的一段很长的普通文本，本不应继续生成。":
                            self.generated += 1
                            yield {"type": "content", "text": char}
                        raise AssertionError("the prose stream should have been closed early")
                    finally:
                        self.closed.set()

                return stream()

        provider = Provider()
        runtime = _runtime(provider)
        emitted = []

        async def emit(kind, payload):
            emitted.append((kind, payload))

        runtime.emit = emit
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-prose")
        runtime._identity_turns[identity] = "turn-a"

        outcome = await runtime._step(identity, {"USER_INPUT_FINALIZED"})

        assert outcome == "recovery_queued"
        assert provider.generated == 10
        assert provider.closed.is_set()
        assert len(runtime.recovery_queue) == 1
        assert runtime.recovery_queue[0].code == "SPEAK_ENVELOPE_INVALID"
        invalid = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "SPEAK_ENVELOPE_INVALID"
        ]
        assert invalid[-1]["error_code"] == "PROSE_INSTEAD_OF_JSON"
        assert invalid[-1]["early_stream_abort"] is True
        assert invalid[-1]["early_stream_abort_non_whitespace_chars"] == 10
        assert invalid[-1]["raw_chars"] == 10

    asyncio.run(scenario())


def test_runtime_does_not_early_abort_fragmented_json_or_bounded_wrappers():
    async def run(raw):
        class Provider:
            supports_decision_schema = True
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.closed = False

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    try:
                        for char in raw:
                            yield {"type": "content", "text": char}
                        yield {"type": "finish", "reason": "completed"}
                    finally:
                        self.closed = True

                return stream()

        provider = Provider()
        runtime = _runtime(provider)
        applied = []

        async def apply(_identity, _turn_id, envelope):
            applied.append(envelope)

        async def dispatch(*_args, **_kwargs):
            return None

        runtime._apply_decision_v06 = apply
        runtime._dispatch_actions_v06 = dispatch
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-json")
        runtime._identity_turns[identity] = "turn-a"

        assert await runtime._step(identity, {"USER_INPUT_FINALIZED"}) == "completed"
        assert provider.closed is True
        assert applied[-1].speak == SpeechDecision("wait", "calm", "")
        assert not runtime.recovery_queue

    strict = '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'
    asyncio.run(run(" " * 64 + strict))
    asyncio.run(run("\ufeff```json\n" + strict + "\n```"))


def test_invalid_json_fingerprint_is_structural_and_does_not_retain_text():
    raw = '```json\n{"speak":{"state":"speak","text":"private words"}'
    fingerprint = json_structure_fingerprint(raw)

    assert fingerprint["json_syntax_valid"] is False
    assert fingerprint["json_first"] == "backtick"
    assert fingerprint["json_last"] == "object_close"
    assert fingerprint["json_fenced"] is True
    assert fingerprint["json_brace_balance"] == 1
    assert "private" not in repr(fingerprint)


def test_valid_json_fingerprint_records_only_contract_shape():
    fingerprint = json_structure_fingerprint(
        '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'
    )

    assert fingerprint == {
        "json_first": "object_open",
        "json_last": "object_close",
        "json_fenced": False,
        "json_brace_balance": 0,
        "json_bracket_balance": 0,
        "json_string_open": False,
        "json_syntax_valid": True,
        "json_top_type": "dict",
        "json_has_speak": True,
        "json_has_actions": True,
        "json_top_key_count": 2,
    }


def test_compatible_parser_combines_only_bounded_json_repairs():
    raw = (
        '\ufeff```json\n'
        '{"speak":{"state":"speak","emotion":"","text":"第一行\n第二行",},}\n'
        '```'
    )
    result = parse_compatible_decision_envelope(raw, allow_empty_control_text=True)

    assert result.envelope.speak == SpeechDecision("speak", "calm", "第一行\n第二行")
    assert result.envelope.actions == ()
    assert set(result.repairs) == {
        "utf8_bom", "single_json_fence", "trailing_comma",
        "string_control_escape", "missing_actions", "empty_emotion",
    }


def test_compatible_parser_keeps_strict_parser_authoritative():
    raw = '{"speak":{"state":"cancel","emotion":"","text":""},"actions":[]}'
    strict = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
    strict.feed(raw)
    with pytest.raises(EnvelopeError, match="invalid speak emotion"):
        strict.finish()

    compatible = parse_compatible_decision_envelope(raw, allow_empty_control_text=True)
    assert compatible.envelope.speak == SpeechDecision("cancel", "calm", "")
    assert compatible.repairs == ("empty_emotion",)


@pytest.mark.parametrize("raw", [
    "这是一段完整的普通回答。",
    '{"speak":{"state":"speak","emotion":"calm","text":"未闭合',
    '说明：{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}',
    '{"speak":{"state":"wait","emotion":"calm","text":""},"extra":true}',
])
def test_compatible_parser_rejects_prose_truncation_mixed_text_and_extra_fields(raw):
    with pytest.raises(EnvelopeError, match="not safely recoverable"):
        parse_compatible_decision_envelope(raw, allow_empty_control_text=True)


def test_new_input_queues_behind_active_recovery_without_cancelling_llm():
    async def scenario():
        cancelled = []

        class Output:
            def cancel_all(self, reason):
                cancelled.append(reason)

        async def emit(*_args):
            return None

        runtime = AgentEpisodeRuntime(
            None,
            SimpleNamespace(),
            Output(),
            emit,
            conversation=ConversationRuntime(),
            ledger=SessionLedger(),
        )
        active_recovery = object()
        runtime.active_recovery = active_recovery
        request = asyncio.create_task(asyncio.sleep(60))
        runtime.active_request = request
        runtime.runner = request

        runtime._wake("USER_INPUT_REFINED")

        await asyncio.sleep(0)
        # Preserve the existing speculative-output invalidation while the
        # provider task itself continues to completion.
        assert cancelled == ["superseded_recovery"]
        assert runtime.active_recovery is active_recovery
        assert request.cancelled() is False
        assert runtime.pending == {"USER_INPUT_REFINED"}

        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("dispatch_kind", ["secondary_mailbox", "final_silence"])
def test_new_final_rearms_silence_without_cancelling_active_system_check(dispatch_kind):
    async def scenario():
        async def emit(*_args):
            return None

        runtime = AgentEpisodeRuntime(
            None,
            SimpleNamespace(),
            SimpleNamespace(status="IDLE", active=False, queue_snapshot=[]),
            emit,
            conversation=ConversationRuntime(),
            ledger=SessionLedger(),
        )
        runtime.episode_id = "episode"
        runtime.state = "WAITING_EVENT"
        runtime.scheduler.arm(0.0)
        runtime.scheduler.advance(30.0 if dispatch_kind == "final_silence" else 4.0)
        assert runtime.scheduler.final_pending is (dispatch_kind == "final_silence")

        request = asyncio.create_task(asyncio.sleep(60))
        runtime.active_request = request
        runtime.runner = request
        runtime.active_dispatch_context = {"dispatch_kind": dispatch_kind, "generation": 1}

        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-new", text="新的用户输入")

        assert request.cancelled() is False
        assert runtime.scheduler.generation == 2
        assert runtime.scheduler.final_pending is False
        assert runtime.pending == {"USER_INPUT_FINALIZED"}

        request.cancel()
        await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())


def test_responses_payload_separates_instructions_and_enables_strict_schema():
    client = DeepSeekClient(
        base_url="https://example.invalid", model="deepseek-v4-flash", api_key="key",
        transport="responses", thinking="disabled",
    )
    schema = decision_schema([])
    payload = client._responses_payload(
        [static("rules"), history("user", "hello")],
        model=client.model, schema=schema, schema_name="voice_decision_v06",
        reasoning_effort="none", max_output_tokens=512,
    )
    assert payload["instructions"].startswith("rules\n\n")
    assert "For wait set speak.text to the empty string" in payload["instructions"]
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"] == schema
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": "hello"}


def test_context_audit_captures_full_wire_payload_and_manifest():
    client = DeepSeekClient(
        base_url="https://example.invalid", model="deepseek-v4-flash", api_key="secret-key",
        transport="responses", thinking="disabled", max_output_tokens=512,
    )
    schema = decision_schema([])
    messages = [static("exact rules"), history("user", "exact user context")]
    snapshot = build_context_snapshot(
        client,
        messages,
        schema,
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        events=["USER_INPUT_FINALIZED"],
        mode="normal",
        reasoning_effort="none",
        context_compaction={},
        context_token_estimate=42,
    )

    expected = client._responses_payload(
        messages,
        model=client.model,
        schema=schema,
        schema_name="voice_decision_v06",
        reasoning_effort="none",
        max_output_tokens=512,
    )
    assert snapshot["wire_sha256"]
    assert snapshot["wire_bytes"] == len(json.dumps(
        expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8"))
    assert snapshot["schema_sha256"]
    assert snapshot["lane_chars"] == {"static_instructions": 11, "history": 18}
    assert snapshot["message_manifest"][1]["content_sha256"]
    assert snapshot["contains_conversation_content"] is True
    assert snapshot["messages"] == messages
    assert snapshot["schema"] == schema
    assert snapshot["wire_payload"] == expected
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "exact user context" in serialized
    assert "exact rules" in serialized
    assert "secret-key" not in serialized
    assert "authorization" not in serialized.casefold()


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
            [history("user", "hello")], schema=decision_schema([]),
        )]
        assert [item["type"] for item in events] == ["content", "content", "usage", "finish"]
        assert events[-2]["usage"]["prompt_tokens"] == 12
        assert events[-2]["usage"]["completion_tokens"] == 7
        assert events[-1]["type"] == "finish"
        assert events[-1]["reason"] == "completed"
        assert events[-1]["stream_metrics"]["sse_event_count"] == 4
        assert events[-1]["stream_metrics"]["sequence_missing_count"] == 4

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
                [history("user", "hello")], schema=decision_schema([]),
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
            [history("user", "hello")], schema=decision_schema([]),
        )]
        assert "".join(item["text"] for item in events if item["type"] == "content") == full
        assert events[-1]["type"] == "finish"
        assert events[-1]["reason"] == "completed"

    asyncio.run(scenario())


def test_responses_stream_observes_malformed_and_non_monotonic_sequences_without_failing():
    async def scenario():
        full = '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'

        class Response:
            status_code = 200

            async def aiter_lines(self):
                yield "data: not-json"
                yield "data: " + json.dumps({
                    "type": "response.output_text.delta", "sequence_number": 1, "delta": full,
                })
                yield 'data: {"type":"response.created","sequence_number":1}'
                yield 'data: {"type":"response.created","sequence_number":0}'
                yield 'data: {"type":"response.completed","sequence_number":4,"response":{"status":"completed"}}'

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
            [history("user", "hello")], schema=decision_schema([]),
        )]
        metrics = events[-1]["stream_metrics"]
        assert metrics["sse_event_count"] == 4
        assert metrics["sse_malformed_count"] == 1
        assert metrics["sequence_duplicate_count"] == 1
        assert metrics["sequence_non_increasing_count"] == 1
        assert metrics["sequence_jump_count"] == 1

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
                [history("user", "hello")], schema=decision_schema([]),
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


def test_responses_runtime_commits_bounded_json_repair_without_recovery(monkeypatch):
    async def scenario():
        raw = (
            '\ufeff```json\n'
            '{"speak":{"state":"wait","emotion":"","text":"",},}\n'
            '```'
        )

        class Provider:
            supports_decision_schema = True
            transport = "responses"
            protocol_recovery_limit = 1

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    yield {"type": "content", "text": raw}
                    yield {"type": "finish", "reason": "completed"}

                return stream()

        runtime = _runtime(Provider())
        emitted = []
        applied = []
        outcomes = []

        monkeypatch.setattr(
            "voice_terminal.agent.episode.capture_context_snapshot",
            lambda *_args, **_kwargs: "wire-digest-a",
        )
        monkeypatch.setattr(
            "voice_terminal.agent.episode.capture_request_outcome",
            lambda **kwargs: outcomes.append(kwargs),
        )

        async def emit(kind, payload):
            emitted.append((kind, payload))

        async def apply(_identity, _turn_id, envelope):
            applied.append(envelope)

        async def dispatch(_identity, _turn_id, _envelope):
            return None

        runtime.emit = emit
        runtime._apply_decision_v06 = apply
        runtime._dispatch_actions_v06 = dispatch
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-a")
        runtime._identity_turns[identity] = "turn-a"

        assert await runtime._step(identity, {"USER_INPUT_FINALIZED"}) == "completed"
        assert applied[0].speak == SpeechDecision("wait", "calm", "")
        assert applied[0].actions == ()
        assert not runtime.recovery_queue
        traces = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "LLM_OUTPUT_COMPAT_RECOVERED"
        ]
        assert len(traces) == 1
        assert set(traces[0]["repairs"]) == {
            "utf8_bom", "single_json_fence", "trailing_comma",
            "missing_actions", "empty_emotion",
        }
        assert "raw" not in traces[0]
        context_traces = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "LLM_CONTEXT_CAPTURED"
        ]
        assert context_traces[0]["context_digest"] == "wire-digest-a"
        completed = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "LLM_REQUEST_COMPLETED"
        ]
        assert completed[0]["validation"] == "compat_recovered"
        assert completed[0]["context_digest"] == "wire-digest-a"
        assert outcomes[0]["wire_sha256"] == "wire-digest-a"
        assert outcomes[0]["outcome"] == "compat_recovered"
        assert outcomes[0]["details"]["output_text"] == raw
        assert outcomes[0]["details"]["reasoning_text"] == ""

    asyncio.run(scenario())


def test_cancelled_request_audit_retains_reason_and_partial_output(monkeypatch):
    async def scenario():
        started = asyncio.Event()

        class Provider:
            supports_decision_schema = True
            transport = "responses"

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    yield {"type": "reasoning", "text": "正在分析"}
                    yield {"type": "content", "text": '{"speak":'}
                    started.set()
                    await asyncio.Event().wait()

                return stream()

        outcomes = []
        monkeypatch.setattr(
            "voice_terminal.agent.episode.capture_context_snapshot",
            lambda *_args, **_kwargs: "wire-digest-cancelled",
        )
        monkeypatch.setattr(
            "voice_terminal.agent.episode.capture_request_outcome",
            lambda **kwargs: outcomes.append(kwargs),
        )
        runtime = _runtime(Provider())
        runtime.inputs["turn-a"] = {"text": "hello", "final": True}
        identity = (runtime.episode_id, "request-cancelled")
        runtime._identity_turns[identity] = "turn-a"
        task = asyncio.create_task(runtime._step(identity, {"USER_INPUT_FINALIZED"}))
        await started.wait()
        task.cancel("superseded_recovery:USER_INPUT_REFINED")
        with pytest.raises(asyncio.CancelledError):
            await task

        assert outcomes[0]["outcome"] == "cancelled"
        assert outcomes[0]["details"] == {
            "cancel_reason": "superseded_recovery:USER_INPUT_REFINED",
            "content_chars": 9,
            "finished": False,
            "reasoning_effort": "none",
            "output_text": '{"speak":',
            "reasoning_text": "正在分析",
        }

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
                    raise DeepSeekHttpError(503, "unavailable")
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


def test_typed_transport_errors_retry_only_before_content_arrives():
    assert DeepSeekHttpError(429).retryable is True
    assert DeepSeekHttpError(503).retryable is True
    assert DeepSeekHttpError(400).retryable is False
    assert DeepSeekTransportError("timeout", received_content=False).retryable is True
    assert DeepSeekTransportError("timeout", received_content=True).retryable is False


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


@pytest.mark.skip(reason="legacy diagnostic shadow chain was removed; production uses shadow_agent component")
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
        await runtime._run_shadow_chain((history("user", "hello"),), "turn-a")
        assert list(runtime.ledger.records) == before
        assert runtime.output.status == "IDLE"
        assert runtime.shadow_results[-1]["valid"] is True
        assert runtime.shadow_results[-1]["state"] == "wait"
        assert runtime.shadow_results[-1]["prompt_tokens"] == 10

    asyncio.run(scenario())




def test_secondary_mailbox_shadow_actions_follow_normal_dispatch_rules():
    async def scenario():
        runtime = _runtime(SimpleNamespace())
        shadow_wakes = []
        runtime.shadow_waker = lambda: shadow_wakes.append("wake") or "started"
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "有必要的新进展。"),
            (Action("request_shadow", {"request": True}),),
        )
        await runtime._dispatch_actions_v06(
            (runtime.episode_id, "request-a"), "turn-a", envelope,
            system_check=True, system_check_id="system-check-a",
            system_check_prompt_kind="secondary_mailbox",
        )
        await asyncio.gather(*tuple(runtime._background_tools))
        assert shadow_wakes == ["wake"]
        assert not any(item["kind"] == "SYSTEM_CHECK_ACTION_REJECTED" for item in runtime.trace)

    asyncio.run(scenario())


def test_final_silence_shadow_actions_are_rejected_before_execution():
    async def scenario():
        runtime = _runtime(SimpleNamespace())
        runtime._system_check_turn_ids.add("system-check-a")
        shadow_wakes = []
        runtime.shadow_waker = lambda: shadow_wakes.append("wake") or "started"
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "系统检查播报。"),
            (
                Action("request_shadow", {"request": True}),
            ),
        )
        await runtime._dispatch_actions_v06(
            (runtime.episode_id, "request-a"), "system-check-a", envelope,
            system_check=True,
            system_check_id="system-check-a",
            system_check_prompt_kind="final_silence",
        )

        assert shadow_wakes == []
        assert runtime._background_tools == set()
        rejected = [item for item in runtime.trace if item["kind"] == "SYSTEM_CHECK_ACTION_REJECTED"]
        assert rejected and rejected[0]["action_names"] == ["request_shadow"]
        assert rejected[0]["prompt_kind"] == "final_silence"

    asyncio.run(scenario())


@pytest.mark.parametrize("dispatch_kind", ["secondary_mailbox", "final_silence"])
def test_system_check_recovery_signal_keeps_immutable_owner_metadata(dispatch_kind):
    async def scenario():
        runtime = _runtime(SimpleNamespace(transport="responses", protocol_recovery_limit=1))
        runtime._system_check_turn_ids.add("system-check-a")
        runtime.active_dispatch_context = {
            "system_check_id": "system-check-a",
            "speech_turn_id": "turn-a",
            "dispatch_kind": dispatch_kind,
            "prompt_kind": dispatch_kind,
            "owner_scope": "system_check",
            "generation": 1,
            "window_index": 3,
        }
        runtime.active_request_identity = (runtime.episode_id, "request-a")
        # Keep this focused on signal construction rather than starting the
        # asynchronous runner in the background.
        runtime.runner = SimpleNamespace(done=lambda: False)

        signal = await runtime._queue_recovery(
            "SPEAK_ENVELOPE_INVALID", "invalid json",
            identity=(runtime.episode_id, "request-a"), turn_id="turn-a",
        )

        assert signal is not None
        assert signal.system_check_id == "system-check-a"
        assert signal.owner_scope == "system_check"
        assert signal.source_turn_id == "turn-a"
        assert signal.dispatch_context["speech_turn_id"] == "turn-a"
        assert signal.dispatch_context["window_index"] == 3

        prompt_kind = runtime._recovery_origin_prompt_kind(signal)
        messages = runtime.messages(
            {"EXCEPTION_RECOVERY", signal.code},
            recovery_signal=signal,
            prompt_kind=prompt_kind,
            force_context=signal.dispatch_context,
            current_turn_id="turn-a",
        )
        tail = request_tail_by_kind(messages)
        feedback = json.loads(tail["immediate_recovery"].removeprefix("即时恢复反馈："))

        assert prompt_kind == dispatch_kind
        assert dispatch_kind in tail
        assert feedback["origin_prompt_kind"] == dispatch_kind
        expected = "不要重新回答历史用户请求" if dispatch_kind == "secondary_mailbox" else "不要把最后一条历史用户输入当成新的请求"
        assert expected in feedback["instruction"]
        assert "重新处理最后一条用户输入" not in feedback["instruction"]

    asyncio.run(scenario())




@pytest.mark.parametrize(
    ("prompt_kind", "expected"),
    [
        ("secondary_mailbox", "不要重新回答历史用户请求"),
        ("final_silence", "不要把最后一条历史用户输入当成新的请求"),
        ("kws_wake", "不要处理历史用户任务"),
    ],
)
def test_system_check_recovery_instruction_preserves_original_scope(prompt_kind, expected):
    instruction = AgentEpisodeRuntime._recovery_instruction(
        "SPEAK_ENVELOPE_INVALID", origin_prompt_kind=prompt_kind,
    )
    assert expected in instruction
    assert "重新处理最后一条用户输入" not in instruction


def test_normal_recovery_reassesses_current_intent_without_replaying_covered_answers():
    instruction = AgentEpisodeRuntime._recovery_instruction("SPEAK_ENVELOPE_INVALID")
    assert "重新评估本轮用户意图和当前队列" in instruction
    assert "不重播已覆盖的回答" in instruction
