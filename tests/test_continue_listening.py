"""通用二等信箱与持续倾听：存储、模型协议和消费驱动状态转换。"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.test_scheduler_staircase import _speaking_runtime, _finish_blocking_request
from tests.test_secondary_mailbox import CapturingProvider, RetryProvider, request_snapshot, wait_for_calls
from voice_terminal.agent.decision_envelope import Action, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.decision_application import ProtocolError
from voice_terminal.agent.prompts import FINAL_SILENCE_PROMPT
from voice_terminal.agent.shadow_registry import VoiceShadowRegistry
from voice_terminal.providers.deepseek import DeepSeekTransportError
from voice_terminal.session.secondary_mailbox import SecondaryMailbox
from voice_terminal.audio_enhancements import AcousticMailbox


def runtime_for(provider=None):
    runtime = _speaking_runtime(provider or CapturingProvider())
    runtime.registry = VoiceShadowRegistry(available=False)
    runtime.ledger.set_shadow_runtime_state(available=False, active=False)
    return runtime


async def request(runtime, count, key="request-a", **kwargs):
    envelope = DecisionEnvelope(SpeechDecision("wait", "calm", ""),
                                (Action("continue_listening", {"count": count}),))
    await runtime._dispatch_actions_v06((runtime.episode_id, key), "turn-a", envelope, **kwargs)


def test_generic_mailbox_open_sources_scopes_versions_and_retry():
    mailbox = SecondaryMailbox()
    scope = mailbox.new_scope()
    first = mailbox.publish(source="future-producer", kind="arbitrary-observation",
                            payload={"value": 1}, scope=scope, coalesce_key="latest")
    captured = mailbox.snapshot()
    newer = mailbox.publish(source="future-producer", kind="arbitrary-observation",
                            payload={"value": 2}, scope=scope, coalesce_key="latest")
    other = mailbox.publish(source="another-producer", kind="another-type", payload={})
    assert mailbox.consume(captured) == []
    assert set(mailbox.snapshot()) == {newer["id"], other["id"]}
    assert mailbox.valid(captured) == captured  # 已替换的冻结项仍可重试。
    mailbox.consume(mailbox.snapshot())
    assert not mailbox
    assert mailbox.valid(captured) == captured  # 已消费的冻结项仍可重试。
    mailbox.invalidate(scope)
    assert mailbox.valid(captured) == {}
    assert first["version"] < newer["version"] < other["version"]


def test_append_without_coalescing_and_payload_isolation():
    mailbox = SecondaryMailbox()
    payload = {"values": [1]}
    first = mailbox.publish(source="source", kind="kind", payload=payload)
    mailbox.publish(source="source", kind="kind", payload=payload)
    payload["values"].append(2)
    first["payload"]["values"].append(3)
    assert len(mailbox.snapshot()) == 2
    assert all(row["payload"] == {"values": [1]} for row in mailbox.snapshot().values())


def test_shadow_invalidation_does_not_clear_other_producers():
    runtime = runtime_for()
    audio = AcousticMailbox(runtime.ledger.secondary_mailbox)
    audio.append(["Dog"], 0, 15600)
    other = runtime.ledger.secondary_mailbox.publish(source="future", kind="observation", payload={})
    runtime.ledger.set_shadow_runtime_state(available=True, active=True, activation_id="a")
    from voice_terminal.shadow_agent.contracts import ShadowEvent
    runtime.ledger.commit_shadow_event(ShadowEvent("p", "progress", "正在核对", 1, "a"))
    snapshot = runtime._secondary_snapshot()
    runtime.ledger.set_shadow_runtime_state(active=False)
    valid = runtime._valid_secondary(snapshot)
    assert {row["source"] for row in valid.values()} == {"acoustic", "future"}
    assert other["id"] in runtime._secondary_snapshot()
    assert audio.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 3])
async def test_exact_count_injections_and_duplicate_acknowledgement(count):
    runtime = runtime_for()
    await request(runtime, count)
    assert runtime.listening.snapshot() == {"remaining": count - 1, "pending": True}
    for index in range(count):
        captured = runtime._secondary_snapshot()
        assert len(captured) == 1
        assert runtime._consume_secondary(captured, prompt_kind="normal") == ["main_llm"]
        state = runtime.listening.snapshot()
        assert state == {"remaining": max(0, count - index - 2), "pending": index < count - 1}
        assert runtime._consume_secondary(captured, prompt_kind="normal") == []
        assert runtime.listening.snapshot() == state
    assert not runtime._secondary_snapshot()
    assert runtime.listening.injected == count


@pytest.mark.asyncio
async def test_append_cap_and_same_decision_is_idempotent():
    runtime = runtime_for()
    await request(runtime, 3, "a")
    await request(runtime, 3, "a")
    assert runtime.listening.snapshot() == {"remaining": 2, "pending": True}
    await request(runtime, 3, "b")
    assert runtime.listening.snapshot() == {"remaining": 4, "pending": True}
    assert runtime.listening.truncated == 1
    await request(runtime, 3, "c")
    assert runtime.listening.remaining == 4
    assert len(runtime._secondary_snapshot()) == 1
    assert runtime.listening.truncated == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, 0, 4, -1, 1.0, "2", None])
async def test_strict_count_validation_has_no_side_effect(value):
    runtime = runtime_for()
    with pytest.raises(ProtocolError, match="count"):
        await request(runtime, value)
    assert not runtime._secondary_snapshot()
    assert runtime.listening.remaining == 0


@pytest.mark.asyncio
async def test_duplicate_action_and_tool_disabled_are_rejected():
    runtime = runtime_for()
    envelope = DecisionEnvelope(SpeechDecision("wait", "calm", ""),
        (Action("continue_listening", {"count": 1}),) * 2)
    with pytest.raises(ProtocolError, match="最多一次"):
        await runtime._dispatch_actions_v06((runtime.episode_id, "a"), "turn-a", envelope)
    runtime.tools_enabled = False
    with pytest.raises(ProtocolError, match="不可用"):
        await request(runtime, 1)
    assert not runtime._secondary_snapshot()


@pytest.mark.asyncio
async def test_refill_waits_for_all_sources_to_be_consumed():
    runtime = runtime_for()
    mailbox = runtime.ledger.secondary_mailbox
    mailbox.publish(source="other", kind="state", payload={"value": 1})
    await request(runtime, 3)
    captured = runtime._secondary_snapshot()
    latest = mailbox.publish(source="other", kind="state", payload={"value": 2})
    runtime._consume_secondary(captured, prompt_kind="normal")
    assert runtime.listening.snapshot() == {"remaining": 2, "pending": False}
    assert set(runtime._secondary_snapshot()) == {latest["id"]}
    runtime._consume_secondary(runtime._secondary_snapshot(), prompt_kind="normal")
    assert runtime.listening.snapshot() == {"remaining": 1, "pending": True}


@pytest.mark.asyncio
async def test_four_second_dispatch_normal_consumption_and_frozen_context():
    provider = CapturingProvider()
    runtime = runtime_for(provider)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await request(runtime, 3)
    await runtime._on_heartbeat(0.4)
    assert provider.calls == 0
    runtime.pending.add("USER_INPUT_FINALIZED")
    await runtime._on_heartbeat(0.8)
    await wait_for_calls(provider, 1)
    snapshot = request_snapshot(provider.messages[-1])
    assert snapshot["runtime"]["secondary_items"] == [{
        "source": "main_llm", "kind": "listening_check", "payload": {}}]
    assert snapshot["runtime"]["listening_request"] == {"remaining": 2, "pending": True}
    assert runtime.listening.snapshot() == {"remaining": 1, "pending": True}
    assert "progress" not in snapshot.get("shadow", {})
    assert "acoustic_events" not in snapshot["runtime"]
    await _finish_blocking_request(runtime, provider)
    await runtime._on_heartbeat(3.6)
    assert provider.calls == 1
    await runtime._on_heartbeat(4.0)
    await wait_for_calls(provider, 2)
    assert runtime.active_dispatch_context["prompt_kind"] == "secondary_mailbox"
    assert runtime.scheduler.anchor_monotonic == 0.0
    assert runtime.max_request_concurrency == 1
    await _finish_blocking_request(runtime, provider)


@pytest.mark.asyncio
async def test_arbitrary_source_alone_dispatches_without_shadow():
    provider = CapturingProvider()
    runtime = runtime_for(provider)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    runtime.ledger.secondary_mailbox.publish(source="new-producer", kind="custom", payload={"value": 7})
    await runtime._on_heartbeat(4.0)
    await wait_for_calls(provider, 1)
    assert request_snapshot(provider.messages[-1])["runtime"]["secondary_items"] == [
        {"source": "new-producer", "kind": "custom", "payload": {"value": 7}}]
    await _finish_blocking_request(runtime, provider)


@pytest.mark.asyncio
async def test_context_failure_does_not_consume_or_refill():
    runtime = runtime_for()
    await request(runtime, 3)
    before = runtime._secondary_snapshot()
    with patch("voice_terminal.agent.episode.capture_context_snapshot", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError, match="audit failed"):
            await runtime._step((runtime.episode_id, "audit"), {"USER_INPUT_FINALIZED"})
    assert runtime._secondary_snapshot() == before
    assert runtime.listening.snapshot() == {"remaining": 2, "pending": True}


@pytest.mark.asyncio
async def test_transport_retry_reuses_count_and_does_not_consume_new_marker():
    provider = RetryProvider()
    runtime = runtime_for(provider)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await request(runtime, 3)
    await runtime._on_heartbeat(4.0)
    task = runtime.active_request
    with pytest.raises(DeepSeekTransportError):
        await task
    first = request_snapshot(provider.messages[-1])
    next_marker = runtime._secondary_snapshot()
    assert runtime.listening.remaining == 1
    await runtime._finish_active_request(task)
    runtime.backoff_until = 0
    await runtime._on_heartbeat(4.4)
    await wait_for_calls(provider, 2)
    retry = request_snapshot(provider.messages[-1])
    assert retry["runtime"]["listening_request"] == first["runtime"]["listening_request"]
    assert runtime._secondary_snapshot() == next_marker
    assert runtime.listening.remaining == 1
    await _finish_blocking_request(runtime, provider)


@pytest.mark.asyncio
async def test_input_stop_reopen_rejects_in_flight_action():
    class ActionProvider(CapturingProvider):
        def stream_events(self, messages, *, json_mode=False):
            self.calls += 1
            self.messages.append(messages)
            async def stream():
                await self.release.wait()
                yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[{"name":"continue_listening","arguments":{"count":3}}]}'}
                yield {"type": "finish", "reason": "stop"}
            return stream()
    provider = ActionProvider()
    runtime = runtime_for(provider)
    assert await runtime._start_dispatch({"USER_INPUT_FINALIZED"})
    await wait_for_calls(provider, 1)
    runtime.close_input_scope()
    runtime.open_input_scope()
    await _finish_blocking_request(runtime, provider)
    assert not runtime._secondary_snapshot()
    assert runtime.listening.remaining == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["protocol", "transport"])
async def test_late_failure_recovery_cannot_inherit_reopened_input_scope(failure):
    class FailingProvider(CapturingProvider):
        def stream_events(self, messages, *, json_mode=False):
            self.calls += 1
            self.messages.append(messages)
            attempt = self.calls
            async def stream():
                await self.release.wait()
                if attempt == 1 and failure == "transport":
                    raise DeepSeekTransportError("timeout", received_content=False)
                count = 0 if attempt == 1 else 3
                yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[{"name":"continue_listening","arguments":{"count":' + str(count) + '}}]}'}
                yield {"type": "finish", "reason": "stop"}
            return stream()
    provider = FailingProvider()
    runtime = runtime_for(provider)
    runtime._ensure_runner = lambda: None
    runtime.scheduler.start_heartbeat(0.0)
    assert await runtime._start_dispatch({"USER_INPUT_FINALIZED"})
    await wait_for_calls(provider, 1)
    old_scope = runtime.listening.scope
    runtime.close_input_scope()
    runtime.open_input_scope()
    task = runtime.active_request
    provider.release.set()
    if failure == "transport":
        with pytest.raises(DeepSeekTransportError):
            await task
    else:
        await task
        assert runtime.recovery_queue[0].listening_context["scope"] == old_scope
    await runtime._finish_active_request(task)
    provider.release.clear()
    if failure == "protocol":
        assert await runtime._start_recovery()
    else:
        runtime.backoff_until = 0
        await runtime._on_heartbeat(0.4)
    await wait_for_calls(provider, 2)
    await _finish_blocking_request(runtime, provider)
    assert runtime.listening.snapshot() == {"remaining": 0, "pending": False}
    assert not runtime._secondary_snapshot()


@pytest.mark.asyncio
async def test_scope_protection_tracks_sources_arriving_during_dispatch_logs():
    provider = CapturingProvider()
    runtime = runtime_for(provider)
    mailbox = runtime.ledger.secondary_mailbox
    late_scope = mailbox.new_scope()
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await request(runtime, 1)
    original_log = runtime.log
    added = False
    async def log(kind, **payload):
        nonlocal added
        if not added and runtime.active_dispatch_context:
            added = True
            mailbox.publish(source="late-producer", kind="observation", payload={}, scope=late_scope)
        await original_log(kind, **payload)
    runtime.log = log
    await runtime._on_heartbeat(4.0)
    await wait_for_calls(provider, 1)
    assert {row["source"] for row in request_snapshot(provider.messages[-1])["runtime"]["secondary_items"]} == {"main_llm", "late-producer"}
    assert late_scope in runtime.active_dispatch_context["secondary_scopes"]
    mailbox.invalidate(late_scope)
    assert runtime._system_check_stale_reason(runtime.active_dispatch_context) == "secondary_scope_expired"
    await _finish_blocking_request(runtime, provider)
    assert {row["source"] for row in runtime._secondary_snapshot().values()} == {"main_llm"}
    await runtime._on_heartbeat(8.0)
    await wait_for_calls(provider, 2)
    assert {row["source"] for row in request_snapshot(provider.messages[-1])["runtime"]["secondary_items"]} == {"main_llm"}
    await _finish_blocking_request(runtime, provider)
    assert runtime.listening.injected == 1


@pytest.mark.asyncio
async def test_model_can_request_from_secondary_evaluation_and_then_wait():
    class ContinuingProvider(CapturingProvider):
        def stream_events(self, messages, *, json_mode=False):
            self.calls += 1
            self.messages.append(messages)
            actions = '[{"name":"continue_listening","arguments":{"count":2}}]' if self.calls == 1 else '[]'
            async def stream():
                await self.release.wait()
                yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":' + actions + '}'}
                yield {"type": "finish", "reason": "stop"}
            return stream()
    provider = ContinuingProvider()
    runtime = runtime_for(provider)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await request(runtime, 1)
    for index, checkpoint in enumerate((4.0, 8.0, 12.0), 1):
        await runtime._on_heartbeat(checkpoint)
        await wait_for_calls(provider, index)
        await _finish_blocking_request(runtime, provider)
    assert runtime.listening.snapshot() == {"remaining": 0, "pending": False}
    assert runtime.listening.injected == 3
    assert runtime._listening_by_identity == {}
    await runtime._on_heartbeat(16.0)
    assert provider.calls == 3
    assert not runtime.ledger.entries  # 标记与 wait（等待）不进入语义历史。
    assert runtime.max_request_concurrency == 1


@pytest.mark.asyncio
async def test_final_dispatch_suspends_refill_and_settlement_clears_plan():
    provider = CapturingProvider()
    runtime = runtime_for(provider)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await request(runtime, 3)
    await runtime._on_heartbeat(30.0)
    await wait_for_calls(provider, 1)
    assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
    assert not runtime.listening.enabled
    assert not runtime._secondary_snapshot()
    assert runtime.listening.remaining == 2
    await _finish_blocking_request(runtime, provider)
    assert runtime.listening.snapshot() == {"remaining": 0, "pending": False}
    assert 'actions 必须为 []' in FINAL_SILENCE_PROMPT


@pytest.mark.asyncio
async def test_merged_schema_strips_fillers_for_each_action():
    runtime = runtime_for()
    runtime.registry = VoiceShadowRegistry(available=True)
    schema = runtime._decision_schema()
    assert schema["properties"]["actions"]["items"]["properties"]["arguments"]["properties"]["count"]["type"] == "integer"
    envelope = DecisionEnvelope(SpeechDecision("wait", "calm", ""),
                                (Action("continue_listening", {"request": True, "count": 2}),))
    normalized = runtime._normalize_schema_envelope(envelope)
    assert normalized.actions[0].arguments == {"count": 2}
    await runtime._dispatch_actions_v06((runtime.episode_id, "schema"), "turn-a", normalized)
    assert runtime.listening.snapshot() == {"remaining": 1, "pending": True}
    runtime.registry = VoiceShadowRegistry(available=True)
    normalized = runtime._normalize_schema_envelope(DecisionEnvelope(
        SpeechDecision("wait", "calm", ""), (Action("request_shadow", {"request": True, "count": 2}),)))
    assert normalized.actions[0].arguments == {"request": True}
    unknown = runtime._normalize_schema_envelope(DecisionEnvelope(
        SpeechDecision("wait", "calm", ""), (Action("continue_listening", {"count": 1, "duration": 10}),)))
    with pytest.raises(ProtocolError, match="count"):
        await runtime._dispatch_actions_v06((runtime.episode_id, "unknown"), "turn-a", unknown)


@pytest.mark.asyncio
async def test_episode_end_reaps_in_flight_listening_context():
    provider = CapturingProvider()
    runtime = runtime_for(provider)
    await request(runtime, 3)
    assert await runtime._start_dispatch({"USER_INPUT_FINALIZED"})
    await wait_for_calls(provider, 1)
    assert runtime._listening_by_identity
    await runtime.end("reset")
    assert runtime._listening_by_identity == {}
    assert runtime._retry_listening_by_turn == {}
    assert not runtime._secondary_snapshot()
    assert runtime.listening.snapshot() == {"remaining": 0, "pending": False}


def test_restore_preserves_new_coalesced_entries_and_single_listening_marker():
    runtime = runtime_for()
    mailbox = runtime.ledger.secondary_mailbox
    runtime.listening.request(2, scope=runtime.listening.scope, logical_id="first")
    scope = mailbox.new_scope()
    mailbox.publish(source="future", kind="state", payload={"value": 1}, scope=scope, coalesce_key="latest")
    captured = mailbox.snapshot()
    runtime._consume_secondary(captured, prompt_kind="normal")
    newer = mailbox.publish(source="future", kind="state", payload={"value": 2}, scope=scope, coalesce_key="latest")
    current_marker = next(row for row in mailbox.snapshot().values() if row["kind"] == "listening_check")
    assert mailbox.restore(captured) == []
    assert set(mailbox.snapshot()) == {newer["id"], current_marker["id"]}
    assert runtime.listening.injected == 2


@pytest.mark.asyncio
async def test_context_cache_and_diagnostics_include_all_producers():
    runtime = runtime_for()
    before = runtime.messages({"USER_INPUT_FINALIZED"})
    schema = runtime._decision_schema()
    await request(runtime, 2)
    audio = AcousticMailbox(runtime.ledger.secondary_mailbox)
    audio.append(["Dog"], 0, 15600)
    runtime.ledger.secondary_mailbox.publish(source="future", kind="observation", payload={})
    after = runtime.messages({"USER_INPUT_FINALIZED"})
    for lane in ("static_instructions", "history"):
        assert [item for item in before if item["context_lane"] == lane] == [
            item for item in after if item["context_lane"] == lane]
    assert runtime._decision_schema() == schema
    assert runtime.snapshot()["secondary_mailbox"]["enqueued"] == 3
    assert runtime.snapshot()["secondary_mailbox"]["pending_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["wait", "speak"])
@pytest.mark.parametrize("with_request", [True, False])
async def test_speech_and_followup_request_are_independent(state, with_request):
    class Handle:
        speech_id = "speech-a"
        text = ""
        finished = False
        async def feed(self, value):
            self.text += value
        async def finish(self):
            self.finished = True
    class Provider(CapturingProvider):
        def stream_events(self, messages, *, json_mode=False):
            self.calls += 1
            self.messages.append(messages)
            async def stream():
                yield {"type": "content", "text": json.dumps({
                    "speak": {"state": state, "emotion": "calm", "text": "还有想补充的吗？" if state == "speak" else ""},
                    "actions": [{"name": "continue_listening", "arguments": {"count": 2}}] if with_request else [],
                }, ensure_ascii=False)}
                yield {"type": "finish", "reason": "stop"}
            return stream()
    runtime = runtime_for(Provider())
    handle = Handle()
    async def begin(*_args, **_kwargs):
        return handle
    runtime.output.begin = begin
    assert await runtime._step((runtime.episode_id, "independent"), {"USER_INPUT_FINALIZED"}) == "completed"
    assert handle.text == ("还有想补充的吗？" if state == "speak" else "")
    assert runtime.listening.snapshot() == {
        "remaining": 1 if with_request else 0, "pending": with_request}
