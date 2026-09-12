from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import (
    FINAL_SILENCE_PROMPT,
    FORCED_POLL_PROMPT,
    AgentDecisionScheduler,
    AgentEpisodeRuntime,
)
from voice_terminal.agent.options import AgentOptions
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

    for window in (3, 6, 9, 16, 22):
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

    value.advance(10.0)
    final = value.forced_due(now=10.0)
    assert final["prompt_kind"] == "final_silence"
    assert final["window_index"] == 25
    assert value.mark_final_pending() is False  # advance already retained it

    # A busy final window remains available until consumed, but it does not
    # create a second final request after the first one is dispatched.
    value.advance(10.4)
    assert value.forced_due(now=10.4)["prompt_kind"] == "final_silence"
    value.mark_forced_dispatched(final, now=10.4)
    assert value.final_dispatched is True
    assert value.armed is False
    assert value.forced_due(now=20.0) is None


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
    value.advance(10.0)
    assert value.final_pending is True
    assert value.cover_forced_by_normal(now=10.0) == [25]
    assert value.final_dispatched is True
    value.advance(10.4)
    assert value.forced_due(now=10.4) is None


def test_pending_ordinary_work_does_not_consume_final_window_before_start():
    value = scheduler()
    value.arm(0.0)
    value.advance(10.0)

    # A forced dispatch that loses the race to ordinary pending work may
    # consume the ordinary forced window, but the final fallback survives
    # until an ordinary request really starts.
    assert value.cover_forced_by_normal(now=10.0, include_final=False) == []
    assert value.final_pending is True
    assert value.forced_due(now=10.4)["prompt_kind"] == "final_silence"

    assert value.cover_forced_by_normal(now=10.4) == [25]
    assert value.final_dispatched is True


def test_long_pause_resets_silence_ladder_and_heartbeat_axis():
    value = scheduler()
    value.arm(0.0, wall_time=100.0)
    value.start_heartbeat(0.0)
    value.advance(8.8)
    old_generation = value.generation

    assert value.reset_after_pause(20.0, wall_time=120.0) is True
    assert value.generation == old_generation + 1
    assert value.anchor_monotonic == 20.0
    assert value.window_index == 0
    assert value.final_pending is False
    assert value.next_heartbeat == pytest.approx(20.4)


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

    forced = "\n".join(
        frame["content"] for frame in runtime.messages(
            {"SYSTEM_CHECK"},
            prompt_kind="forced_poll",
            force_context={
                "anchor_wall_time": 123.0,
                "silence_elapsed_ms": 1200,
                "window_index": 3,
                "skipped_forced_windows": 0,
            },
        )
    )
    final = "\n".join(
        frame["content"] for frame in runtime.messages(
            {"SYSTEM_CHECK"},
            prompt_kind="final_silence",
            force_context={
                "anchor_wall_time": 123.0,
                "silence_elapsed_ms": 10000,
                "window_index": 25,
                "skipped_forced_windows": 2,
            },
        )
    )
    normal = "\n".join(frame["content"] for frame in runtime.messages({"USER_INPUT_FINALIZED"}))

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

        await runtime.end("reset")

        assert runtime.runner is None
        assert runtime.active_request is None
        assert runtime.scheduler.next_heartbeat is None
        assert runtime.scheduler.armed is False

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
        await asyncio.sleep(0.08)

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


def test_speaking_timer_only_candidates_remain_gated():
    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime.user_speaking = True
        runtime.scheduler.start_heartbeat(9.6)

        runtime.scheduler.arm(8.79)
        await runtime._on_heartbeat(10.0)
        assert provider.calls == 0
        assert runtime.scheduler.snapshot()["skipped_windows"] == [3]

        runtime.scheduler.arm(0.39)
        await runtime._on_heartbeat(10.4)
        assert provider.calls == 0
        assert runtime.scheduler.final_pending is True
        assert runtime.scheduler.final_dispatched is False

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

        await runtime.output_completed({
            "speech_id": "speech-a",
            "conversation_id": "conversation-a",
            "request_id": "request-a",
            "turn_id": "system-check-a",
            "text": "我还在这里。",
            "status": "completed",
        })

        speech = next(payload for kind, payload in emitted if kind == "speech.completed")
        assert speech["owner_scope"] == "system_check"
        assert speech["system_check_id"] == "system-check-a"
        assert not any(kind == "agent.turn_settled" for kind, _payload in emitted)
        assert runtime._system_check_turn_ids == set()

    asyncio.run(scenario())
