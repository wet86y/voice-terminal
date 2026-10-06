from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import (
    FINAL_SILENCE_PROMPT,
    SECONDARY_MAILBOX_PROMPT,
    AgentDecisionScheduler,
    AgentEpisodeRuntime,
    EpisodeHeartbeatClock,
    RecoverySignal,
)
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.shadow_agent.contracts import ShadowEvent


def scheduler() -> AgentDecisionScheduler:
    return AgentDecisionScheduler(AgentOptions(scheduler_tick_ms=400))


async def _shadow_progress(runtime, text: str, event_id: str, *, source="agent", activation_id="activation-a") -> None:
    await runtime.accept_shadow_event(ShadowEvent(
        event_id, "progress", text, 1, activation_id=activation_id, source=source,
    ))


class _BlockingProvider:
    transport = "responses"
    protocol_recovery_limit = 1

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    def stream_events(self, _messages, *, json_mode=False):
        assert json_mode is True
        self.calls += 1

        async def stream():
            await self.release.wait()
            yield {"type": "content", "text": '{"speak":{"state":"wait"},"actions":[]}'}
            yield {"type": "finish", "reason": "stop"}

        return stream()


def _speaking_runtime(provider, *, defer_silence_until_output=False):
    class Registry:
        def begin_turn(self, _episode_id):
            return None

        def cancel_turn(self, _episode_id):
            return None

        def action_definitions(self):
            return []

    class Output:
        active = False
        status = "IDLE"
        queue_snapshot = []
        speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

        def cancel_all(self, _reason):
            return None

        async def close(self):
            return None

    async def emit(*_args):
        return None

    runtime = AgentEpisodeRuntime(
        provider, Registry(), Output(), emit,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
        defer_silence_until_output=defer_silence_until_output,
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "你好", "final": False}
    runtime.ledger.set_shadow_runtime_state(available=True, active=True, activation_id="activation-a")
    return runtime


async def _finish_blocking_request(runtime, provider):
    request = runtime.active_request
    assert request is not None
    provider.release.set()
    await asyncio.wait_for(request, timeout=1)
    await runtime._finish_active_request(request)
    provider.release.clear()


def test_late_heartbeat_is_rebased_to_real_time_without_catch_up():
    value = scheduler()
    assert value.start_heartbeat(10.0) == 10.4
    assert value.heartbeat_delay(10.1) == pytest.approx(0.3)

    # A late callback evaluates once at the real callback time.  It must not
    # leave a backlog of historical 400ms deadlines to replay.
    value.heartbeat(10.9)
    assert value.next_heartbeat == pytest.approx(11.3)


def test_secondary_checkpoints_do_not_dispatch_without_mailbox_and_final_is_single_shot():
    value = scheduler()
    value.arm(0.0, wall_time=123.0)
    value.advance(3.6)
    assert value.secondary_due(0) is False
    value.advance(4.0)
    assert value.secondary_due(9) is True
    assert value.secondary_candidate(now=4.0)["window_index"] == 10
    assert value.forced_due(now=4.0) is None
    value.advance(8.0)
    assert value.secondary_due(10) is True
    assert value.forced_due(now=8.0) is None

    value.advance(30.0)
    final = value.forced_due(now=30.0)
    assert final["prompt_kind"] == "final_silence"
    assert final["window_index"] == 75
    assert value.mark_final_pending() is False  # advance already retained it

    # A busy final window remains available until consumed, but it does not
    # create a second final request after the first one is dispatched.
    value.advance(30.4)
    assert value.forced_due(now=30.4)["prompt_kind"] == "final_silence"
    value.mark_forced_dispatched(final, now=30.4)
    assert value.final_dispatched is True
    assert value.armed is False
    assert value.forced_due(now=20.0) is None


def test_secondary_progress_coalesces_and_only_dispatches_at_four_second_checkpoint():
    async def scenario():
        class CapturingProvider(_BlockingProvider):
            def stream_events(self, messages, *, json_mode=False):
                self.messages = messages
                return super().stream_events(messages, json_mode=json_mode)

        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await runtime._on_heartbeat(0.4)
        assert provider.calls == 0

        await _shadow_progress(runtime, "正在查找", "progress-1")
        await _shadow_progress(runtime, "正在查找", "progress-2")
        await _shadow_progress(runtime, "正在核对", "progress-3")
        assert runtime._secondary_pending == {"agent": 2}
        assert runtime.snapshot()["secondary_mailbox"]["enqueued"] == 2
        await runtime._on_heartbeat(3.6)
        assert provider.calls == 0
        await runtime._on_heartbeat(4.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.active_dispatch_context["prompt_kind"] == "secondary_mailbox"
        assert runtime._secondary_pending == {}
        packet = json.loads(provider.messages[-2]["content"])
        entries = {item["kind"]: item["content"] for item in packet["entries"]}
        assert "secondary_mailbox" in entries
        assert "agent" in entries["secondary_update"]
        state = json.loads(entries["runtime_state"].split("\n工具可用性：")[0].removeprefix("当前轮运行状态："))
        assert state["shadow"]["progress"] == [{"text": "正在核对", "source": "agent"}]
        assert all(entry.kind != "shadow_event" for entry in runtime.ledger.entries)
        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(8.0)
        assert provider.calls == 1

    asyncio.run(scenario())


def test_ordinary_request_carries_and_consumes_secondary_progress():
    async def scenario():
        class CapturingProvider(_BlockingProvider):
            def stream_events(self, messages, *, json_mode=False):
                self.messages = messages
                return super().stream_events(messages, json_mode=json_mode)

        provider = CapturingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "已取得新进度", "progress-1")
        runtime.pending.add("USER_INPUT_REFINED")
        await runtime._on_heartbeat(0.4)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.active_dispatch_context is None
        assert runtime._secondary_pending == {}
        assert runtime.snapshot()["secondary_mailbox"]["consumed_normal"] == 1
        packet = json.loads(provider.messages[-2]["content"])
        assert "secondary_update" in {item["kind"] for item in packet["entries"]}
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_secondary_version_ack_keeps_updates_arriving_after_context_snapshot():
    runtime = _speaking_runtime(_BlockingProvider())
    runtime.ledger.commit_shadow_event(ShadowEvent("p1", "progress", "第一版", 1, "activation-a"))
    captured = runtime._secondary_snapshot()
    runtime.ledger.commit_shadow_event(ShadowEvent("p2", "progress", "第二版", 1, "activation-a"))
    assert runtime._consume_secondary(captured, prompt_kind="normal") == []
    assert runtime._secondary_pending == {"agent": 2}
    assert runtime._consume_secondary(runtime._secondary_snapshot(), prompt_kind="normal") == ["agent"]
    assert runtime._secondary_pending == {}


def test_secondary_progress_survives_context_build_failure():
    async def scenario():
        runtime = _speaking_runtime(_BlockingProvider())
        await _shadow_progress(runtime, "待处理进度", "progress-1")

        def fail_context(*_args, **_kwargs):
            raise RuntimeError("context unavailable")

        runtime.messages = fail_context
        with pytest.raises(RuntimeError, match="context unavailable"):
            await runtime._step((runtime.episode_id, "step-a"), {"SYSTEM_CHECK"})
        assert runtime._secondary_pending == {"agent": 1}

    asyncio.run(scenario())


def test_secondary_progress_arriving_during_context_build_keeps_new_version():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        await _shadow_progress(runtime, "第一版", "progress-1")
        original_messages = runtime.messages

        def add_new_version(*args, **kwargs):
            frames = original_messages(*args, **kwargs)
            runtime.ledger.commit_shadow_event(ShadowEvent("p2", "progress", "第二版", 1, "activation-a"))
            return frames

        runtime.messages = add_new_version
        runtime.pending.add("SHADOW_EVENT")
        runtime.scheduler.start_heartbeat(0.0)
        await runtime._on_heartbeat(0.4)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime._secondary_pending == {"agent": 2}
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_speaking_or_active_request_retains_secondary_progress_for_later_checkpoint():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "进度一", "progress-1")
        runtime.user_speaking = True
        await runtime._on_heartbeat(4.0)
        assert provider.calls == 0
        assert runtime._secondary_pending

        runtime.user_speaking = False
        await runtime._on_heartbeat(8.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        await _shadow_progress(runtime, "进度二", "progress-2")
        await runtime._on_heartbeat(12.0)
        assert provider.calls == 1
        assert runtime._secondary_pending
        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(16.0)
        for _ in range(10):
            if provider.calls == 2:
                break
            await asyncio.sleep(0)
        assert provider.calls == 2
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_backoff_retains_secondary_progress_until_next_checkpoint():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "待评估进度", "progress-1")
        runtime.backoff_until = time.monotonic() + 60
        await runtime._on_heartbeat(4.0)
        assert provider.calls == 0
        assert runtime._secondary_pending == {"agent": 1}
        assert runtime.snapshot()["secondary_mailbox"]["skipped_busy"] == 1

        runtime.backoff_until = 0
        await runtime._on_heartbeat(8.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_ending_episode_clears_secondary_progress():
    async def scenario():
        runtime = _speaking_runtime(_BlockingProvider())
        await _shadow_progress(runtime, "尚未处理", "progress-1")
        assert runtime._secondary_pending
        await runtime.end("reset")
        assert runtime._secondary_pending == {}
        assert runtime.ledger._shadow_progress_seen == {}

    asyncio.run(scenario())


def test_final_silence_wins_over_secondary_mailbox_at_same_checkpoint():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler = AgentDecisionScheduler(AgentOptions(final_silence_ms=8000))
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await _shadow_progress(runtime, "尚有进度", "progress-1")
        await runtime._on_heartbeat(8.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
        assert runtime.scheduler.final_dispatched is True
        assert runtime.snapshot()["secondary_mailbox"]["cleared_final"] == 1
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_final_settlement_clears_progress_arriving_during_final_request():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler = AgentDecisionScheduler(AgentOptions(final_silence_ms=8000))
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        await runtime._on_heartbeat(8.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        await _shadow_progress(runtime, "最终检查中到达", "progress-1")
        assert runtime._secondary_pending
        await _finish_blocking_request(runtime, provider)
        assert runtime._secondary_pending == {}
        assert runtime.snapshot()["secondary_mailbox"]["cleared_final"] == 1

    asyncio.run(scenario())


def test_ordinary_request_at_final_deadline_keeps_final_pending_until_idle():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.scheduler = AgentDecisionScheduler(AgentOptions(final_silence_ms=8000))
        runtime.scheduler.start_heartbeat(0.0)
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        runtime.pending.add("SHADOW_EVENT")
        await runtime._on_heartbeat(8.0)
        for _ in range(10):
            if provider.calls:
                break
            await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.active_dispatch_context is None
        assert runtime.scheduler.final_pending is True
        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(8.4)
        for _ in range(10):
            if provider.calls == 2:
                break
            await asyncio.sleep(0)
        assert provider.calls == 2
        assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_deferred_scheduler_activates_same_generation_from_later_playback():
    value = scheduler()
    generation = value.defer(0.0, wall_time=100.0, turn_id="turn-a")

    assert generation == 1
    assert value.armed is False
    assert value.is_deferred(generation) is True
    assert value.forced_due(now=100.0) is None

    assert value.activate_deferred(
        45.0,
        wall_time=145.0,
        turn_id="turn-a",
        generation=generation,
        anchor_source="tts_playback_completed",
    ) is True
    assert value.generation == generation
    assert value.anchor_monotonic == 45.0
    assert value.anchor_source == "tts_playback_completed"

    value.advance(74.99)
    assert value.forced_due(now=74.99) is None
    value.advance(75.0)
    candidate = value.forced_due(now=75.0)
    assert candidate["prompt_kind"] == "final_silence"
    assert candidate["silence_elapsed_ms"] == 30000
    assert candidate["generation"] == generation
    assert candidate["anchor_source"] == "tts_playback_completed"


def test_deferred_runtime_uses_user_silence_when_playback_completed_earlier():
    async def scenario():
        runtime = _speaking_runtime(
            _BlockingProvider(), defer_silence_until_output=True,
        )
        user_silence = time.monotonic()
        await runtime.observe(
            "USER_INPUT_FINALIZED",
            turn_id="turn-a",
            text="你好",
            input_finished_at=user_silence,
        )
        pending = runtime._pending_silence_anchor
        assert pending is not None
        pending["request_id"] = "request-a"
        generation = pending["generation"]

        assert await runtime.playback_completed(
            turn_id="turn-a",
            conversation_id=runtime.episode_id,
            request_id="request-a",
            playback_completed_at=user_silence - 1.0,
            wall_time=100.0,
        ) is True
        assert runtime.scheduler.generation == generation
        assert runtime.scheduler.anchor_source == "user_silence"
        assert runtime.scheduler.anchor_monotonic == user_silence
        assert runtime._pending_silence_anchor is None
        await runtime.stop()

    asyncio.run(scenario())


def test_deferred_runtime_uses_transaction_settlement_without_audio():
    async def scenario():
        runtime = _speaking_runtime(
            _BlockingProvider(), defer_silence_until_output=True,
        )
        await runtime.observe(
            "USER_INPUT_FINALIZED",
            turn_id="turn-a",
            text="请继续",
        )
        pending = runtime._pending_silence_anchor
        assert pending is not None
        pending["request_id"] = "request-a"
        runtime.output.busy = True

        assert await runtime._settle_pending_silence_after_request(
            (runtime.episode_id, "request-a"), "turn-a", "completed",
        ) is False
        assert runtime.scheduler.armed is False
        assert pending["waiting_for_output"] is True

        runtime.output.busy = False
        assert await runtime.output_transaction_settled({
            "request_id": "request-a", "turn_id": "turn-a", "status": "failed",
            "speech_id": "speech-a",
        }) is True
        assert runtime.scheduler.armed is True
        assert runtime.scheduler.anchor_source == "transaction_settled"
        assert runtime._pending_silence_anchor is None
        await runtime.stop()

    asyncio.run(scenario())


def test_deferred_runtime_wait_response_arms_after_request_settles():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(
            provider, defer_silence_until_output=True,
        )
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="请听着")
        for _ in range(50):
            if provider.calls:
                break
            await asyncio.sleep(0.01)
        assert provider.calls == 1
        assert runtime.scheduler.armed is False

        await _finish_blocking_request(runtime, provider)
        assert runtime.scheduler.armed is True
        assert runtime.scheduler.anchor_source == "transaction_settled"
        assert runtime._pending_silence_anchor is None
        await runtime.stop()

    asyncio.run(scenario())


def test_deferred_runtime_rebinds_retry_request_identity_before_settlement():
    async def scenario():
        runtime = _speaking_runtime(
            _BlockingProvider(), defer_silence_until_output=True,
        )
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="重试这句")
        pending = runtime._pending_silence_anchor
        assert pending is not None
        pending["request_id"] = "request-a"
        runtime.output.busy = True

        # A transport retry/recovery request owns the same user turn but has
        # a new request identity.  Its settlement must not be rejected as a
        # stale callback for the first request.
        runtime._bind_pending_silence_request(
            (runtime.episode_id, "request-b"), "turn-a", allow_rebind=True,
        )
        assert pending["request_id"] == "request-b"
        assert await runtime._settle_pending_silence_after_request(
            (runtime.episode_id, "request-b"), "turn-a", "completed",
        ) is False
        assert pending["waiting_for_output"] is True

        runtime.output.busy = False
        assert await runtime.output_transaction_settled({
            "request_id": "request-b", "turn_id": "turn-a", "status": "failed",
        }) is True
        assert runtime.scheduler.anchor_source == "transaction_settled"
        await runtime.stop()

    asyncio.run(scenario())


def test_deferred_runtime_ignores_old_playback_after_new_input_generation():
    async def scenario():
        runtime = _speaking_runtime(
            _BlockingProvider(), defer_silence_until_output=True,
        )
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="第一句")
        old = runtime._pending_silence_anchor
        assert old is not None
        old["request_id"] = "request-a"
        old_generation = old["generation"]

        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-b", text="第二句")
        current = runtime._pending_silence_anchor
        assert current is not None
        assert current["generation"] != old_generation
        assert await runtime.playback_completed(
            turn_id="turn-a",
            conversation_id=runtime.episode_id,
            request_id="request-a",
            playback_completed_at=time.monotonic() + 45.0,
        ) is False
        assert runtime.scheduler.armed is False
        assert runtime._pending_silence_anchor is current
        await runtime.stop()

    asyncio.run(scenario())


def test_kws_wake_system_check_is_immediate_and_does_not_create_user_input():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.inputs.clear()
        runtime.last_turn_id = None

        generation = await runtime.schedule_system_check(
            "kws_wake", "wake-a", arm_silence=True,
        )
        assert generation == runtime.scheduler.generation
        assert runtime.inputs == {}
        assert runtime.last_turn_id is None
        assert runtime.scheduler.anchor_turn_id == "wake-a"

        now = runtime.scheduler.anchor_monotonic
        runtime.scheduler.start_heartbeat(now)
        await runtime._on_heartbeat(now + 0.4)
        await asyncio.sleep(0)

        assert provider.calls == 1
        assert runtime.active_request_source_turn_id == "wake-a"
        assert runtime.active_dispatch_context["prompt_kind"] == "kws_wake"
        assert runtime.active_dispatch_context["source_id"] == "wake-a"
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_final_system_check_settlement_is_exactly_once_and_generation_guarded():
    async def run_one(*, supersede: bool):
        provider = _BlockingProvider()
        settled = []
        runtime = _speaking_runtime(provider)
        runtime.on_final_system_check_settled = lambda context: settled.append(context)
        runtime.scheduler.arm(0.0, turn_id="wake-a")
        runtime.scheduler.advance(30.0)
        candidate = runtime.scheduler.forced_due(now=30.0)
        assert candidate["prompt_kind"] == "final_silence"
        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate)
        await asyncio.sleep(0)
        if supersede:
            runtime.scheduler.arm(30.1, turn_id="turn-new")
        await _finish_blocking_request(runtime, provider)
        await runtime._finish_active_request(None)
        return settled

    assert len(asyncio.run(run_one(supersede=False))) == 1
    assert asyncio.run(run_one(supersede=True)) == []


def test_final_speak_settles_only_when_output_transaction_completes():
    async def scenario():
        settled = []
        runtime = _speaking_runtime(_BlockingProvider())
        runtime.on_final_system_check_settled = lambda context: settled.append(context)
        runtime.scheduler.arm(0.0, turn_id="wake-a")
        context = {"prompt_kind": "final_silence", "generation": 1}
        runtime._final_system_checks["system-check-a"] = context
        runtime._system_check_turn_ids.add("system-check-a")
        runtime._system_check_output_turn_ids.add("system-check-a")
        runtime._system_check_speeches["speech-a"] = {
            "system_check_id": "system-check-a",
            "speech_turn_id": "wake-a",
            "scheduler_generation": 1,
            "prompt_kind": "final_silence",
        }

        assert settled == []
        record = {
            "speech_id": "speech-a", "turn_id": "wake-a", "status": "completed",
            "conversation_id": runtime.episode_id, "request_id": "request-a",
            "text": "稍后见", "delivery": {"status": "full", "heard_text": "稍后见"},
        }
        await runtime.output_completed(record)
        await runtime.output_completed(record)
        assert len(settled) == 1

    asyncio.run(scenario())


def test_no_fixed_poll_windows_are_created():
    value = scheduler()
    value.arm(0.0)
    value.advance(1.2)
    assert value.forced_due(now=1.2, has_normal=True) is None
    assert value.forced_due(now=1.2) is None

    value.advance(2.4)
    assert value.forced_due(now=2.4) is None


def test_long_pause_keeps_anchor_without_replaying_secondary_checkpoints():
    value = scheduler()
    value.arm(0.0, wall_time=100.0, turn_id="turn-a")
    value.start_heartbeat(0.0)
    old_generation = value.generation

    value.advance(8.8)
    assert value.forced_due(now=8.8) is None
    assert value.secondary_due(0) is True
    assert value.secondary_candidate(now=8.8)["speech_turn_id"] == "turn-a"
    assert value.generation == old_generation
    assert value.anchor_monotonic == 0.0
    assert value.anchor_wall_time == 100.0
    assert value.snapshot()["resolutions"] == {}

    value.advance(30.0)
    final = value.forced_due(now=30.0)
    assert final["prompt_kind"] == "final_silence"
    assert value.secondary_candidate(now=30.0) is None
    assert value.generation == old_generation


def test_secondary_and_final_prompts_are_mutually_exclusive():
    class Registry:
        def action_definitions(self):
            return []

    async def emit(*_args):
        return None

    output = SimpleNamespace(
        active=False,
        queue_snapshot=[],
        speak_snapshot={"queue_status": "empty", "text_only_queue": True},
    )
    runtime = AgentEpisodeRuntime(
        None, Registry(), output, emit,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "你好", "final": True}

    secondary_messages = runtime.messages(
        {"SYSTEM_CHECK"},
        prompt_kind="secondary_mailbox",
        force_context={
            "anchor_wall_time": 123.0,
            "silence_elapsed_ms": 4000,
            "window_index": 10,
        },
    )
    final_messages = runtime.messages(
        {"SYSTEM_CHECK"},
        prompt_kind="final_silence",
        force_context={
            "anchor_wall_time": 123.0,
            "silence_elapsed_ms": 30000,
            "window_index": 75,
        },
    )

    def tail_contents(messages):
        packet = json.loads(messages[-2]["content"])
        return "\n".join(entry["content"] for entry in packet["entries"])

    secondary = tail_contents(secondary_messages)
    final = tail_contents(final_messages)
    normal = tail_contents(runtime.messages({"USER_INPUT_FINALIZED"}))

    assert json.loads(secondary_messages[-2]["content"])["entries"][-1]["kind"] == "secondary_mailbox"
    assert json.loads(final_messages[-2]["content"])["entries"][-1]["kind"] == "final_silence"
    assert SECONDARY_MAILBOX_PROMPT in secondary
    assert "系统即将进入静默状态" not in secondary
    assert "actions 必须为 []" not in secondary
    assert "没有必要的新内容时必须 wait（保持等待）" in secondary
    assert "actions（动作列表）与 speak（语音决策）独立判断" in secondary
    assert FINAL_SILENCE_PROMPT in final
    assert SECONDARY_MAILBOX_PROMPT not in final
    assert "actions 必须为 []" in final
    assert "进入静默本身不是需要播报的事件" in final
    assert SECONDARY_MAILBOX_PROMPT not in normal
    assert FINAL_SILENCE_PROMPT not in normal


def test_secondary_mailbox_protocol_recovery_keeps_the_secondary_mailbox_request_scope():
    async def scenario():
        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.messages = None

            def stream_events(self, messages, *, json_mode=False):
                assert json_mode is True
                self.messages = messages

                async def stream():
                    yield {
                        "type": "content",
                        "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}',
                    }
                    yield {"type": "finish", "reason": "stop"}

                return stream()

        provider = Provider()
        runtime = _speaking_runtime(provider)
        signal = RecoverySignal(
            "SPEAK_ENVELOPE_INVALID",
            "invalid json",
            source_request_id="request-a",
            source_turn_id="turn-a",
            system_check_id="system-check-a",
            owner_scope="system_check",
            dispatch_context={
                "system_check_id": "system-check-a",
                "speech_turn_id": "turn-a",
                "dispatch_kind": "secondary_mailbox",
                "prompt_kind": "secondary_mailbox",
                "owner_scope": "system_check",
                "generation": runtime.scheduler.generation,
                "window_index": 3,
                "silence_elapsed_ms": 1200,
            },
        )

        assert await runtime._start_dispatch(
            {"EXCEPTION_RECOVERY", signal.code}, recovery_signal=signal,
        )
        request = runtime.active_request
        assert request is not None
        assert await asyncio.wait_for(request, timeout=1) == "completed"
        await runtime._finish_active_request(request)

        packet = json.loads(provider.messages[-2]["content"])
        entries = {entry["kind"]: entry["content"] for entry in packet["entries"]}
        assert "secondary_mailbox" in entries
        assert SECONDARY_MAILBOX_PROMPT in entries["secondary_mailbox"]
        assert "本次没有新的用户输入" in entries["immediate_recovery"]
        assert "重新处理最后一条用户输入" not in entries["immediate_recovery"]

    asyncio.run(scenario())


def test_tts_events_are_not_scheduler_events():
    value = scheduler()
    for kind in ("TTS_STARTED", "TTS_COMPLETED", "TTS_REPLACED", "TTS_FAILED", "TTS_HELD"):
        assert value.accepts(kind) is False
    assert value.accepts("USER_INPUT_FINALIZED") is True


def test_episode_end_clears_the_runner_and_heartbeat_axis():
    async def scenario():
        class Registry:
            def cancel_turn(self, _episode_id):
                return None

            def action_definitions(self):
                return []

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def cancel_all(self, _reason):
                return None

        async def emit(*_args):
            return None

        runtime = AgentEpisodeRuntime(
            None, Registry(), Output(), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.episode_id = "conversation-a"
        runtime._wake("SPEECH_STARTED")
        await asyncio.sleep(0)
        assert runtime.runner is not None and not runtime.runner.done()
        clock = runtime._heartbeat_clock
        assert clock is not None and clock.alive

        await runtime.end("reset")

        assert runtime.runner is None
        assert runtime.active_request is None
        assert runtime.scheduler.next_heartbeat is None
        assert runtime.scheduler.armed is False
        assert runtime._heartbeat_clock is None
        assert clock.alive is False

    asyncio.run(scenario())


def test_runtime_heartbeat_continues_while_active_request_is_running():
    async def scenario():
        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.calls = 0
                self.release = asyncio.Event()

            def stream_events(self, _messages, *, json_mode=False):
                assert json_mode is True
                self.calls += 1

                async def stream():
                    await self.release.wait()
                    return
                    yield  # pragma: no cover

                return stream()

        class Registry:
            def begin_turn(self, _episode_id):
                return None

            def cancel_turn(self, _episode_id):
                return None

            def action_definitions(self):
                return []

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def cancel_all(self, _reason):
                return None

            async def close(self):
                return None

        async def emit(*_args):
            return None

        provider = Provider()
        runtime = AgentEpisodeRuntime(
            provider, Registry(), Output(), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        # The production option remains 400 ms; shortening only the injected
        # clock in this test makes the non-blocking property fast to verify.
        runtime.scheduler.interval = 0.01
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="你好")
        for _ in range(50):
            if provider.calls:
                break
            await asyncio.sleep(0.01)
        for _ in range(100):
            if runtime.scheduler.window_index >= 6:
                break
            await asyncio.sleep(0.01)

        assert provider.calls == 1
        assert runtime.active_request is not None
        assert runtime.scheduler.window_index >= 6
        # The active provider does not stop the heartbeat or create another request.
        assert runtime.scheduler.snapshot()["window_index"] >= 6
        assert runtime.runner is not None and not runtime.runner.done()

        provider.release.set()
        await runtime.stop()

    asyncio.run(scenario())


def test_speaking_semantic_mailbox_dispatches_once_and_coalesces():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.user_speaking = True
        runtime.pending.update({"USER_SEGMENT_UPDATED", "TOOL_COMPLETED"})

        await runtime._on_heartbeat(10.0)
        await asyncio.sleep(0)

        assert provider.calls == 1
        assert runtime.active_request_kinds == {"USER_SEGMENT_UPDATED", "TOOL_COMPLETED"}
        assert runtime.pending == set()
        assert runtime.max_request_concurrency == 1

        # Extra mailbox wakeups cannot create a concurrent request while the
        # first semantic precomputation remains active.
        await runtime._on_heartbeat(10.4)
        assert provider.calls == 1
        assert runtime.max_request_concurrency == 1

        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_speaking_skips_secondary_check_but_not_final_silence():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.user_speaking = True
        runtime.scheduler.start_heartbeat(9.6)

        runtime.scheduler.arm(6.0, turn_id="turn-a")
        await _shadow_progress(runtime, "尚未评估", "progress-1")
        await runtime._on_heartbeat(10.0)
        await asyncio.sleep(0)
        assert provider.calls == 0
        assert runtime._secondary_pending == {"agent": 1}
        assert runtime.snapshot()["secondary_mailbox"]["skipped_speaking"] == 1

        runtime.scheduler.arm(-19.61, turn_id="turn-a")
        await runtime._on_heartbeat(10.4)
        await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.scheduler.final_pending is False
        assert runtime.scheduler.final_dispatched is True
        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_speaking_leaves_new_semantic_work_pending_until_active_request_finishes():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.pending.add("USER_SEGMENT_UPDATED")

        await runtime._on_heartbeat(10.0)
        await asyncio.sleep(0)
        assert provider.calls == 1

        runtime.user_speaking = True
        runtime.pending.add("TOOL_COMPLETED")
        await runtime._on_heartbeat(10.4)
        assert provider.calls == 1
        assert runtime.pending == {"TOOL_COMPLETED"}

        await _finish_blocking_request(runtime, provider)
        await runtime._on_heartbeat(10.8)
        await asyncio.sleep(0)
        assert provider.calls == 2
        assert runtime.active_request_kinds == {"TOOL_COMPLETED"}
        assert runtime.max_request_concurrency == 1

        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_system_check_delivery_is_tagged_without_user_turn_settlement():
    async def scenario():
        emitted = []

        class Registry:
            def action_definitions(self):
                return []

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

        async def emit(kind, payload):
            emitted.append((kind, payload))

        runtime = AgentEpisodeRuntime(
            None, Registry(), Output(), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
        )
        runtime.episode_id = "conversation-a"
        runtime.agent_step_id = "request-a"
        runtime._system_check_turn_ids.add("system-check-a")
        runtime._system_check_output_turn_ids.add("system-check-a")
        runtime._system_check_speeches["speech-a"] = {
            "system_check_id": "system-check-a",
            "speech_turn_id": "turn-a",
            "scheduler_generation": 3,
        }

        await runtime.output_completed({
            "speech_id": "speech-a",
            "conversation_id": "conversation-a",
            "request_id": "request-a",
            "turn_id": "turn-a",
            "text": "我还在这里。",
            "status": "completed",
        })

        speech = next(payload for kind, payload in emitted if kind == "speech.completed")
        assert speech["owner_scope"] == "system_check"
        assert speech["system_check_id"] == "system-check-a"
        assert speech["turn_id"] == "turn-a"
        assert speech["speech_turn_id"] == "turn-a"
        assert speech["scheduler_generation"] == 3
        assert not any(kind == "agent.turn_settled" for kind, _payload in emitted)
        assert runtime._system_check_turn_ids == set()

    asyncio.run(scenario())


def test_tts_state_never_blocks_llm_dispatch_authorization():
    runtime = _speaking_runtime(_BlockingProvider())
    runtime.output.active = True
    runtime.output.busy = True
    runtime.output.status = "SPEAKING"
    runtime.output.queue_snapshot = [{"speech_id": "speech-a"}]
    runtime.user_speaking = True

    assert runtime._dispatch_allowed() is True
    assert runtime._busy_reason() == "not_allowed"


@pytest.mark.parametrize("prompt_kind", ["secondary_mailbox", "final_silence"])
def test_system_check_keeps_system_identity_but_uses_current_speech_turn(prompt_kind):
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.output.speak_snapshot = {
            "queue_status": "speaking",
            "conversation_id": runtime.episode_id,
            "turn_id": "turn-a",
            "speech_id": "speech-a",
            "output_generation": 1,
            "text": "我先说明第一点。",
        }
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        if prompt_kind == "secondary_mailbox":
            await _shadow_progress(runtime, "待评估进度", "progress-1")
            runtime.scheduler.advance(4.0)
            candidate = runtime.scheduler.secondary_candidate(now=4.0)
        else:
            runtime.scheduler.advance(30.0)
            candidate = runtime.scheduler.forced_due(now=30.0)

        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate) is True
        await asyncio.sleep(0)

        context = runtime.active_dispatch_context
        assert context["system_check_id"].startswith("system-check-")
        assert context["speech_turn_id"] == "turn-a"
        assert runtime.active_request_source_turn_id == "turn-a"
        assert runtime._last_model_speak_state == {
            "state": "speaking",
            "owner_scope": "current_turn",
            "continuation_mode": "append_only",
            "text_parts": {
                "committed_prefix": "我先说明第一点。",
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            },
        }

        await _finish_blocking_request(runtime, provider)

    asyncio.run(scenario())


def test_system_check_extension_uses_existing_append_transaction():
    async def scenario():
        class Handle:
            speech_id = "speech-suffix"

            async def finish(self):
                return None

        class Output:
            active = True
            status = "SPEAKING"
            queue_snapshot = []
            speak_snapshot = {
                "queue_status": "speaking",
                "conversation_id": "conversation-a",
                "turn_id": "turn-a",
                "speech_id": "speech-base",
                "output_generation": 4,
                "text": "已有回答",
            }

            def __init__(self):
                self.appended = []

            async def append_successor(self, identity, turn_id, **kwargs):
                self.appended.append((identity, turn_id, kwargs))
                return Handle()

            async def replace_current(self, *_args, **_kwargs):
                raise AssertionError("a strict extension must not replace speech")

        runtime = _speaking_runtime(_BlockingProvider())
        runtime.episode_id = "conversation-a"
        runtime.output = Output()
        context = {
            "owner_scope": "system_check",
            "system_check_id": "system-check-a",
            "speech_turn_id": "turn-a",
            "generation": 1,
        }
        envelope = DecisionEnvelope(
            SpeechDecision("speak", "calm", "已有回答，补充一点。"), (),
        )

        await runtime._apply_decision_v06(
            ("conversation-a", "request-a"), "turn-a", envelope,
            dispatch_context=context,
        )

        assert len(runtime.output.appended) == 1
        identity, turn_id, kwargs = runtime.output.appended[0]
        assert identity == ("conversation-a", "request-a")
        assert turn_id == "turn-a"
        assert kwargs["suffix"] == "，补充一点。"
        assert runtime._system_check_speeches["speech-suffix"] == {
            "system_check_id": "system-check-a",
            "speech_turn_id": "turn-a",
            "scheduler_generation": 1,
        }

    asyncio.run(scenario())


def test_thread_heartbeat_coalesces_while_event_loop_is_blocked():
    async def scenario():
        loop = asyncio.get_running_loop()
        pulses = []
        clock = EpisodeHeartbeatClock(
            interval=0.01,
            episode_generation=7,
            loop=loop,
            callback=pulses.append,
        )
        clock.start()
        try:
            # Intentionally block only this test loop.  The heartbeat thread
            # must continue advancing its absolute sequence meanwhile.
            import time
            time.sleep(0.065)
            await asyncio.sleep(0.02)
        finally:
            clock.stop()
            await asyncio.to_thread(clock.join)

        assert pulses
        assert pulses[0].episode_generation == 7
        assert pulses[0].sequence >= 2
        assert pulses[0].coalesced_count >= 1

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt_kind", ["secondary_mailbox", "final_silence"])
def test_new_scheduler_generation_discards_late_system_check_speech(prompt_kind):
    async def scenario():
        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            def stream_events(self, _messages, *, json_mode=False):
                assert json_mode is True

                async def stream():
                    self.started.set()
                    await self.release.wait()
                    yield {
                        "type": "content",
                        "text": '{"speak":{"state":"speak","emotion":"calm","text":"旧检查输出"},"actions":[]}',
                    }
                    yield {"type": "finish", "reason": "stop"}

                return stream()

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def __init__(self):
                self.begin_calls = 0

            async def begin(self, *_args, **_kwargs):
                self.begin_calls += 1
                raise AssertionError("stale system-check speech must not enter output")

            def cancel_all(self, _reason):
                return None

        emitted = []

        async def emit(kind, payload):
            emitted.append((kind, payload))

        provider = Provider()
        runtime = _speaking_runtime(provider)
        runtime.output = Output()
        runtime.emit = emit
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        if prompt_kind == "secondary_mailbox":
            await _shadow_progress(runtime, "待评估进度", "progress-1")
            runtime.scheduler.advance(4.0)
            candidate = runtime.scheduler.secondary_candidate(now=4.0)
        else:
            runtime.scheduler.advance(30.0)
            candidate = runtime.scheduler.forced_due(now=30.0)

        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate)
        request = runtime.active_request
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        runtime.scheduler.arm(2.0, turn_id="turn-a")
        provider.release.set()
        assert await asyncio.wait_for(request, timeout=1) == "system_check_stale"
        await runtime._finish_active_request(request)

        assert runtime.output.begin_calls == 0
        stale = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "SYSTEM_CHECK_STALE"
        ]
        assert len(stale) == 1
        assert stale[0]["speech_turn_id"] == "turn-a"
        assert stale[0]["stale_system_check_output_discarded"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt_kind", ["secondary_mailbox", "final_silence"])
def test_user_speech_onset_discards_late_system_check_speech_before_asr_final(prompt_kind):
    async def scenario():
        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            def stream_events(self, _messages, *, json_mode=False):
                assert json_mode is True

                async def stream():
                    self.started.set()
                    await self.release.wait()
                    yield {
                        "type": "content",
                        "text": '{"speak":{"state":"speak","emotion":"calm","text":"旧检查输出"},"actions":[]}',
                    }
                    yield {"type": "finish", "reason": "stop"}

                return stream()

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def __init__(self):
                self.begin_calls = 0
                self.held = 0

            async def begin(self, *_args, **_kwargs):
                self.begin_calls += 1
                raise AssertionError("speech from a pre-onset system check must remain inaudible")

            def hold_unplayed(self):
                self.held += 1

            def cancel_all(self, _reason):
                return None

        emitted = []

        async def emit(kind, payload):
            emitted.append((kind, payload))

        provider = Provider()
        runtime = _speaking_runtime(provider)
        runtime.output = Output()
        runtime.emit = emit
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        if prompt_kind == "secondary_mailbox":
            await _shadow_progress(runtime, "待评估进度", "progress-1")
            runtime.scheduler.advance(4.0)
            candidate = runtime.scheduler.secondary_candidate(now=4.0)
        else:
            runtime.scheduler.advance(30.0)
            candidate = runtime.scheduler.forced_due(now=30.0)

        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate)
        request = runtime.active_request
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        dispatch_epoch = runtime.active_dispatch_context["user_speech_epoch"]

        # No ASR final or scheduler re-arm has happened.  Speech onset alone
        # must invalidate only the old system-check output.
        await runtime.speech_activity(True)
        assert runtime.scheduler.generation == 1
        assert runtime._user_speech_epoch == dispatch_epoch + 1
        assert runtime.output.held == 1
        assert not request.cancelled()

        provider.release.set()
        assert await asyncio.wait_for(request, timeout=1) == "system_check_stale"
        await runtime._finish_active_request(request)
        assert runtime.output.begin_calls == 0

        stale = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "SYSTEM_CHECK_STALE"
        ]
        assert stale[-1]["stale_reason"] == "user_speech_started_after_dispatch"

        if runtime.voice_activation_task and not runtime.voice_activation_task.done():
            runtime.voice_activation_task.cancel()
            await asyncio.gather(runtime.voice_activation_task, return_exceptions=True)

    asyncio.run(scenario())


def test_system_check_started_while_user_speaks_never_releases_old_answer():
    async def scenario():
        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self):
                self.release = asyncio.Event()

            def stream_events(self, _messages, *, json_mode=False):
                assert json_mode is True

                async def stream():
                    await self.release.wait()
                    yield {
                        "type": "content",
                        "text": '{"speak":{"state":"speak","emotion":"calm","text":"旧检查输出"},"actions":[]}',
                    }
                    yield {"type": "finish", "reason": "stop"}

                return stream()

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            def __init__(self):
                self.begin_calls = 0

            async def begin(self, *_args, **_kwargs):
                self.begin_calls += 1
                raise AssertionError("a system check dispatched during speech must not queue TTS")

            def cancel_all(self, _reason):
                return None

        emitted = []

        async def emit(kind, payload):
            emitted.append((kind, payload))

        provider = Provider()
        runtime = _speaking_runtime(provider)
        runtime.output = Output()
        runtime.emit = emit
        runtime.user_speaking = True
        runtime._user_speech_epoch = 4
        runtime.scheduler.arm(0.0, turn_id="turn-a")
        runtime.scheduler.advance(30.0)
        candidate = runtime.scheduler.forced_due(now=30.0)

        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate)
        request = runtime.active_request
        assert runtime.active_dispatch_context["user_speaking_at_dispatch"] is True

        # This reproduces the observed ordering: the timer request begins in
        # the middle of an utterance, then the user stops before ASR final.
        await runtime.speech_activity(False)
        provider.release.set()
        assert await asyncio.wait_for(request, timeout=1) == "system_check_stale"
        await runtime._finish_active_request(request)
        assert runtime.output.begin_calls == 0

        stale = [
            payload for kind, payload in emitted
            if kind == "agent.trace" and payload.get("kind") == "SYSTEM_CHECK_STALE"
        ]
        assert stale[-1]["stale_reason"] == "user_speaking_at_dispatch"

    asyncio.run(scenario())


def test_user_speech_onset_withdraws_only_unplayed_system_check_candidate():
    async def scenario():
        class Workspace:
            playback_started = False

        class Handle:
            def __init__(self, speech_id):
                self.speech_id = speech_id
                self.workspace = Workspace()

        class Output:
            active = False
            status = "IDLE"
            queue_snapshot = []
            speak_snapshot = {"queue_status": "queued", "text_only_queue": True}

            def __init__(self):
                self.handle = Handle("ordinary-speech")
                self._pending = [Handle("system-speech")]
                self.cancelled = []
                self.held = 0

            def hold_unplayed(self):
                self.held += 1

            def cancel(self, speech_id, reason):
                self.cancelled.append((speech_id, reason))
                return True

            def cancel_all(self, _reason):
                raise AssertionError("passive onset must not cancel the ordinary output scope")

        runtime = _speaking_runtime(_BlockingProvider())
        runtime.output = Output()
        runtime._system_check_speeches["system-speech"] = {
            "system_check_id": "system-check-a",
            "speech_turn_id": "turn-a",
            "scheduler_generation": runtime.scheduler.generation,
        }

        await runtime.speech_activity(True)

        assert runtime.output.held == 1
        assert runtime.output.cancelled == [
            ("system-speech", "superseded_system_check"),
        ]

        if runtime.voice_activation_task and not runtime.voice_activation_task.done():
            runtime.voice_activation_task.cancel()
            await asyncio.gather(runtime.voice_activation_task, return_exceptions=True)

    asyncio.run(scenario())


def test_arm_silence_window_arms_only_an_idle_ladder():
    """A model-initiated KWS→ASR rotation gets the final-silence ladder."""

    async def scenario():
        runtime = _speaking_runtime(_BlockingProvider())
        generation = await runtime.arm_silence_window("wake-task")
        assert generation == runtime.scheduler.generation
        assert runtime.scheduler.armed is True
        assert runtime.scheduler.anchor_turn_id == "wake-task"
        assert runtime.scheduler.anchor_source == "model_speech"

        # An armed ladder owns the handover: no second window may be stacked.
        assert await runtime.arm_silence_window("wake-task") is None
        assert runtime.scheduler.generation == generation

        # The final window carries exactly the generation the settlement guard
        # compares against, so a superseded ladder can never rearm KWS.
        anchor = runtime.scheduler.anchor_monotonic
        at = anchor + runtime.scheduler.FINAL_WINDOW * runtime.scheduler.interval
        runtime.scheduler.advance(at)
        candidate = runtime.scheduler.forced_due(now=at)
        assert candidate["prompt_kind"] == "final_silence"
        assert candidate["generation"] == generation
        await runtime.stop()

    asyncio.run(scenario())


def test_arm_silence_window_leaves_a_user_ladder_alone():
    """KWS sessions defer the user ladder until its playback boundary."""

    async def scenario():
        runtime = _speaking_runtime(
            _BlockingProvider(), defer_silence_until_output=True,
        )
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="你好")
        assert runtime.scheduler.is_deferred() is True

        assert await runtime.arm_silence_window("wake-task") is None
        assert runtime.scheduler.armed is False, "延迟中的用户梯子不得被替换"
        await runtime.stop()

    asyncio.run(scenario())
