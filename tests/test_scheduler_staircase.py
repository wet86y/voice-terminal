from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import (
    FINAL_SILENCE_PROMPT,
    FORCED_POLL_PROMPT,
    AgentDecisionScheduler,
    AgentEpisodeRuntime,
    EpisodeHeartbeatClock,
)
from voice_terminal.agent.options import AgentOptions
from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def scheduler() -> AgentDecisionScheduler:
    return AgentDecisionScheduler(AgentOptions(scheduler_tick_ms=400))


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


def _speaking_runtime(provider):
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
    )
    runtime.episode_id = runtime.conversation.conversation_id
    runtime.last_turn_id = "turn-a"
    runtime.inputs["turn-a"] = {"text": "你好", "final": False}
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


def test_staircase_windows_and_final_dispatch_are_single_shot():
    value = scheduler()
    value.arm(0.0, wall_time=123.0)

    for window in (3, 6, 9, 16, 22, 28, 34, 40, 46, 52, 58, 64, 70):
        if window == 16:
            value.advance(4.0)
            assert value.window_index == 10
            assert value.forced_due(now=4.0) is None
        value.advance(window * 0.4)
        candidate = value.forced_due(now=window * 0.4)
        assert candidate == {
            "prompt_kind": "forced_poll",
            "dispatch_kind": "forced_poll",
            "window_index": window,
            "silence_elapsed_ms": window * 400,
            "skipped_forced_windows": 0,
            "anchor_wall_time": 123.0,
            "generation": 1,
        }
        value.mark_forced_dispatched(candidate, now=window * 0.4)

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


def test_ordinary_dispatch_covers_forced_window_without_replaying_it():
    value = scheduler()
    value.arm(0.0)
    value.advance(1.2)
    assert value.forced_due(now=1.2, has_normal=True) is None
    assert value.cover_forced_by_normal(now=1.2) == [3]
    assert value.forced_due(now=1.2) is None

    value.advance(2.4)
    next_candidate = value.forced_due(now=2.4)
    assert next_candidate["window_index"] == 6


def test_final_window_covered_by_ordinary_work_does_not_repeat():
    value = scheduler()
    value.arm(0.0)
    value.advance(30.0)
    assert value.final_pending is True
    assert value.cover_forced_by_normal(now=30.0) == [75]
    assert value.final_dispatched is True
    value.advance(30.4)
    assert value.forced_due(now=30.4) is None


def test_pending_ordinary_work_does_not_consume_final_window_before_start():
    value = scheduler()
    value.arm(0.0)
    value.advance(30.0)

    # A forced dispatch that loses the race to ordinary pending work may
    # consume the ordinary forced window, but the final fallback survives
    # until an ordinary request really starts.
    assert value.cover_forced_by_normal(now=30.0, include_final=False) == []
    assert value.final_pending is True
    assert value.forced_due(now=30.4)["prompt_kind"] == "final_silence"

    assert value.cover_forced_by_normal(now=30.4) == [75]
    assert value.final_dispatched is True


def test_long_pause_coalesces_to_latest_checkpoint_without_resetting_anchor():
    value = scheduler()
    value.arm(0.0, wall_time=100.0, turn_id="turn-a")
    value.start_heartbeat(0.0)
    old_generation = value.generation

    value.advance(8.8)
    candidate = value.forced_due(now=8.8)

    assert candidate["window_index"] == 22
    assert candidate["speech_turn_id"] == "turn-a"
    assert value.take_newly_coalesced_windows() == [3, 6, 9, 16]
    assert value.generation == old_generation
    assert value.anchor_monotonic == 0.0
    assert value.anchor_wall_time == 100.0
    assert value.snapshot()["resolutions"] == {
        3: "coalesced_after_delay",
        6: "coalesced_after_delay",
        9: "coalesced_after_delay",
        16: "coalesced_after_delay",
    }

    value.advance(30.0)
    final = value.forced_due(now=30.0)
    assert final["prompt_kind"] == "final_silence"
    assert value.take_newly_coalesced_windows() == [28, 34, 40, 46, 52, 58, 64, 70, 22]
    assert value.generation == old_generation


def test_force_and_final_prompts_are_mutually_exclusive():
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

    forced_messages = runtime.messages(
        {"SYSTEM_CHECK"},
        prompt_kind="forced_poll",
        force_context={
            "anchor_wall_time": 123.0,
            "silence_elapsed_ms": 1200,
            "window_index": 3,
            "skipped_forced_windows": 0,
        },
    )
    final_messages = runtime.messages(
        {"SYSTEM_CHECK"},
        prompt_kind="final_silence",
        force_context={
            "anchor_wall_time": 123.0,
            "silence_elapsed_ms": 30000,
            "window_index": 75,
            "skipped_forced_windows": 2,
        },
    )

    def tail_contents(messages):
        packet = json.loads(messages[-2]["content"])
        return "\n".join(entry["content"] for entry in packet["entries"])

    forced = tail_contents(forced_messages)
    final = tail_contents(final_messages)
    normal = tail_contents(runtime.messages({"USER_INPUT_FINALIZED"}))

    assert json.loads(forced_messages[-2]["content"])["entries"][-1]["kind"] == "forced_poll"
    assert json.loads(final_messages[-2]["content"])["entries"][-1]["kind"] == "final_silence"
    assert FORCED_POLL_PROMPT in forced
    assert "系统即将进入静默状态" not in forced
    assert FINAL_SILENCE_PROMPT in final
    assert "系统400ms调度器的强制检查窗口触发" not in final
    assert FORCED_POLL_PROMPT not in normal
    assert FINAL_SILENCE_PROMPT not in normal


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
        # The active provider does not stop the scheduler loop; any skipped
        # windows remain valid, single-shot staircase windows.
        assert set(runtime.scheduler.snapshot()["skipped_windows"]).issubset(
            set(runtime.scheduler.FORCE_WINDOWS)
        )
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


def test_speaking_does_not_gate_timer_only_candidates():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.user_speaking = True
        runtime.scheduler.start_heartbeat(9.6)

        runtime.scheduler.arm(8.79, turn_id="turn-a")
        await runtime._on_heartbeat(10.0)
        await asyncio.sleep(0)
        assert provider.calls == 1
        assert runtime.active_dispatch_context["system_check_id"].startswith("system-check-")
        assert runtime.active_dispatch_context["speech_turn_id"] == "turn-a"
        await _finish_blocking_request(runtime, provider)

        runtime.scheduler.arm(-19.61, turn_id="turn-a")
        await runtime._on_heartbeat(10.4)
        await asyncio.sleep(0)
        assert provider.calls == 2
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


def test_system_check_keeps_system_identity_but_uses_current_speech_turn():
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
        runtime.scheduler.advance(1.2)
        candidate = runtime.scheduler.forced_due(now=1.2)

        assert await runtime._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate) is True
        await asyncio.sleep(0)

        context = runtime.active_dispatch_context
        assert context["system_check_id"].startswith("system-check-")
        assert context["speech_turn_id"] == "turn-a"
        assert runtime.active_request_source_turn_id == "turn-a"
        assert runtime._last_model_speak_state == {
            "state": "speaking",
            "owner_scope": "current_turn",
            "continuation_allowed": True,
            "target_text": "我先说明第一点。",
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


def test_new_scheduler_generation_discards_late_system_check_speech():
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
        runtime.scheduler.advance(1.2)
        candidate = runtime.scheduler.forced_due(now=1.2)

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


def test_user_speech_onset_discards_late_system_check_speech_before_asr_final():
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
        runtime.scheduler.advance(1.2)
        candidate = runtime.scheduler.forced_due(now=1.2)

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
        runtime.scheduler.advance(1.2)
        candidate = runtime.scheduler.forced_due(now=1.2)

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
