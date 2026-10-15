"""续写窗口回归：真实发送、设备播放、推理权限与送达结算共同验证。"""

import asyncio
import json
import time

import pytest

from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.tts.router import TtsBackendRouter
from tests.test_speech_continuation import (
    ContinuationHarness, FIRST_TEXT, SUFFIX_TEXT, HANDOFF_TIMEOUT_SECONDS, _agent,
)


async def revise(harness, runtime, text, snapshot=None):
    """以原请求快照提交 revision（尾巴修订），保留真实原子边界校验。"""
    return await runtime._apply_revision_tail(
        harness.identity, harness.turn_id,
        snapshot if snapshot is not None else dict(harness.output.speak_snapshot),
        text, emotion="calm",
    )


def model_state(harness):
    """model_state（模型可见状态）：核实权限及不重叠的文字分区。"""
    return AgentEpisodeRuntime._model_speak_state(
        harness.output.speak_snapshot,
        conversation_id=harness.identity[0], turn_id=harness.turn_id,
    )


async def complete(harness, handle):
    """complete（消费实际音频并等待送达结算）。"""
    harness.render()
    await asyncio.wait_for(asyncio.shield(handle.task), HANDOFF_TIMEOUT_SECONDS)


def expire(handle):
    """expire（推进窗口截止点）：不引入真实的一秒等待。"""
    handle.continuation_deadline = time.monotonic() - 1
    handle.continuation_signal.set()


def test_closed_sender_accepts_empty_tail_and_plays_only_suffix_after_settlement():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            assert first.admission.closed and first.sent_chars == len(FIRST_TEXT)
            state = model_state(harness)
            assert state["continuation_mode"] == "rewrite_tail"
            assert state["text_parts"]["revisable_tail"] == ""
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            assert first.text == FIRST_TEXT
            assert first.continuation_tail == SUFFIX_TEXT
            assert harness.tts.sessions[0].sent == [FIRST_TEXT]
            assert harness.output.speak_snapshot["text"] == FIRST_TEXT + SUFFIX_TEXT
            assert model_state(harness)["text_parts"]["revisable_tail"] == SUFFIX_TEXT
            # 接受的尾巴不能使旧句已听记录膨胀；历史结算完成前也不能抢播放器。
            harness.settlement_gate = asyncio.Event()
            harness.render()
            await asyncio.wait_for(harness.settlement_started.wait(), HANDOFF_TIMEOUT_SECONDS)
            assert len(harness.tts.sessions) == 1
            assert harness.records[0]["text"] == FIRST_TEXT
            assert harness.records[0]["delivery"]["heard_text"] == FIRST_TEXT
            # 已接受内容不因慢结算跨过窗口截止点而丢失。
            expire(first)
            harness.settlement_gate.set()
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert second.speech_id != first.speech_id
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            assert harness.render() == b"\x02\x00" * 80
            await asyncio.wait_for(asyncio.shield(second.task), HANDOFF_TIMEOUT_SECONDS)
            assert [record["delivery"]["heard_text"] for record in harness.records] == [FIRST_TEXT, SUFFIX_TEXT]
            assert harness.output.delivered["text"] == FIRST_TEXT + SUFFIX_TEXT
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_one_second_window_starts_at_actual_output_and_does_not_delay_ready_suffix():
    async def scenario():
        harness = ContinuationHarness(device_latency_ms=40)
        runtime = _agent(harness)
        try:
            first = await harness.first()
            assert first.continuation_deadline is None
            await complete(harness, first)
            completed_at = first.workspace.timings["playback_output_completed_at"]
            assert first.continuation_deadline == completed_at + 1.0
            state = model_state(harness)
            assert state["state"] == "continuation"
            assert harness.output.continuation_active
            assert state["owner_scope"] == "current_turn"
            assert state["continuation_mode"] == "rewrite_tail"
            assert state["text_parts"] == {
                "committed_prefix": FIRST_TEXT, "current_clause": "",
                "prefetched_clause": "", "revisable_tail": "",
            }
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert time.monotonic() < first.continuation_deadline
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            await complete(harness, second)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("request_after_playback", [False, True])
def test_request_authorized_in_window_can_finish_after_deadline(request_after_playback):
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            if request_after_playback:
                await complete(harness, first)
            async with harness.output.inference_boundary.request(harness.identity):
                snapshot = dict(harness.output.speak_snapshot)
                assert first.continuation_tickets
                if not request_after_playback:
                    await complete(harness, first)
                expire(first)
                assert model_state(harness)["continuation_mode"] == "rewrite_tail"
                assert await revise(harness, runtime, SUFFIX_TEXT, snapshot) is first
            assert not first.continuation_tickets
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            await complete(harness, second)
            assert harness.output.delivered["text"] == FIRST_TEXT + SUFFIX_TEXT
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_expired_window_rejects_old_revision_and_allows_fresh_same_turn_speech():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            snapshot = dict(harness.output.speak_snapshot)
            await complete(harness, first)
            expire(first)
            assert harness.output.speak_snapshot["queue_status"] == "empty"
            assert await revise(harness, runtime, SUFFIX_TEXT, snapshot) is None
            await runtime._apply_decision_v06(harness.identity, harness.turn_id,
                DecisionEnvelope(SpeechDecision("speak", "calm", SUFFIX_TEXT), ()))
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == [SUFFIX_TEXT]
            await complete(harness, second)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_request_after_deadline_cannot_borrow_an_earlier_requests_authority():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            await complete(harness, first)
            async with harness.output.inference_boundary.request(harness.identity):
                original = dict(harness.output.speak_snapshot)
                expire(first)
                assert len(first.continuation_tickets) == 1
                async with harness.output.inference_boundary.request(harness.identity):
                    assert len(first.continuation_tickets) == 1
                    assert model_state(harness)["state"] == "continuation"
                    assert model_state(harness)["continuation_mode"] == "none"
                    assert await revise(harness, runtime, "不得借用其他请求的权限。", original) is None
                assert model_state(harness)["continuation_mode"] == "rewrite_tail"
                assert await revise(harness, runtime, SUFFIX_TEXT, original) is first
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            await complete(harness, second)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_pending_tail_can_be_replaced_but_stale_snapshot_cannot_overwrite_it():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            original = dict(harness.output.speak_snapshot)
            assert await revise(harness, runtime, SUFFIX_TEXT, original) is first
            revised = dict(harness.output.speak_snapshot)
            assert await revise(harness, runtime, "过期的尾巴。", original) is None
            assert await revise(harness, runtime, "图片里有一只猫。", revised) is first
            assert first.text_revision == 2
            await complete(harness, first)
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == ["图片里有一只猫。"]
            await complete(harness, second)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["playing", "settling", "window"])
@pytest.mark.parametrize("reason", ["interrupted", "reset", "stopped"])
def test_cancellation_revokes_pending_tail_and_inflight_permission(phase, reason):
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            if phase == "window":
                await complete(harness, first)
            async with harness.output.inference_boundary.request(harness.identity):
                snapshot = dict(harness.output.speak_snapshot)
                if phase != "window":
                    assert await revise(harness, runtime, SUFFIX_TEXT, snapshot) is first
                if phase == "settling":
                    harness.settlement_gate = asyncio.Event()
                    harness.render()
                    await asyncio.wait_for(harness.settlement_started.wait(), HANDOFF_TIMEOUT_SECONDS)
                harness.output.cancel_all(reason)
                assert not first.continuation_tail
                assert not first.continuation_tickets
                assert await revise(harness, runtime, "不得复活。", snapshot) is None
            if harness.settlement_gate is not None:
                harness.settlement_gate.set()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert len(harness.tts.sessions) == 1
            assert harness.output.speak_snapshot["queue_status"] == "empty"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_turn_change_revokes_window_without_requiring_next_user_to_wait():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            first = await harness.first()
            await complete(harness, first)
            snapshot = dict(harness.output.speak_snapshot)
            harness.runtime.last_turn_id = "next-turn"
            assert harness.output.speak_snapshot["queue_status"] == "empty"
            assert await revise(harness, runtime, SUFFIX_TEXT, snapshot) is None
            next_handle = await harness.output.begin(harness.identity, "next-turn")
            await next_handle.feed("新问题的回答。")
            await next_handle.finish()
            await harness.wait_for_drain(next_handle)
            await complete(harness, next_handle)
            assert harness.tts.sessions[1].sent == ["新问题的回答。"]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalidation", ["interrupt", "turn_change", "cancel_by_id"])
def test_handoff_preparation_remains_cancellable_and_rechecks_turn(invalidation):
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            preparing.set()
            await release.wait()

        try:
            first = await harness.first()
            await complete(harness, first)
            harness.output.controller._on_model_speech_accepted = prepare
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            await asyncio.wait_for(preparing.wait(), HANDOFF_TIMEOUT_SECONDS)
            worker = harness.output._continuation_task
            if invalidation == "interrupt":
                harness.output.cancel_all("interrupted")
            elif invalidation == "cancel_by_id":
                assert harness.output.cancel(first.speech_id)
            else:
                harness.runtime.last_turn_id = "next-turn"
            release.set()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), HANDOFF_TIMEOUT_SECONDS)
            assert len(harness.tts.sessions) == 1
            assert harness.output.handle is None
            assert harness.output.speak_snapshot["queue_status"] == "empty"
            assert first.resources_released
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


def test_revision_during_handoff_preparation_plays_latest_tail_only():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare():
            preparing.set()
            await release.wait()

        try:
            first = await harness.first()
            await complete(harness, first)
            harness.output.controller._on_model_speech_accepted = prepare
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            await asyncio.wait_for(preparing.wait(), HANDOFF_TIMEOUT_SECONDS)
            assert await revise(harness, runtime, "准备期间更新的最终结论。") is first
            release.set()
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == ["准备期间更新的最终结论。"]
            await complete(harness, second)
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


def test_cancelling_handoff_during_emotion_preparation_cleans_registered_successor():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare_emotion(emotion, **_kwargs):
            preparing.set()
            await release.wait()
            return emotion

        try:
            first = await harness.first()
            await complete(harness, first)
            harness.tts.prepare_emotion = prepare_emotion
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            await asyncio.wait_for(preparing.wait(), HANDOFF_TIMEOUT_SECONDS)
            worker = harness.output._continuation_task
            second = harness.output.handle
            assert second is not None and second is not first
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, second.task, return_exceptions=True), HANDOFF_TIMEOUT_SECONDS)
            assert second.reason == "cancelled"
            assert not harness.output.busy
            assert first.resources_released and second.resources_released
            assert harness.output.handle is None
            assert not harness.output._pending
            assert not harness.output._handles_by_token
            assert len(harness.tts.sessions) == 1
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


def test_empty_window_expiration_releases_resources_without_new_audio():
    async def scenario():
        harness = ContinuationHarness()
        _agent(harness)
        try:
            first = await harness.first()
            await complete(harness, first)
            worker = harness.output._continuation_task
            assert harness.output.continuation_active
            assert not first.resources_released
            expire(first)
            await asyncio.wait_for(asyncio.shield(worker), HANDOFF_TIMEOUT_SECONDS)
            assert not harness.output.continuation_active
            assert first.resources_released
            assert len(harness.tts.sessions) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_kind", ["failure", "cancel"])
def test_failed_or_cancelled_inference_releases_window_and_submission_gate(exit_kind):
    async def scenario():
        harness = ContinuationHarness()
        _agent(harness)
        entered, finish = asyncio.Event(), asyncio.Event()
        try:
            first = await harness.first()
            await complete(harness, first)

            async def request():
                async with harness.output.inference_boundary.request(harness.identity):
                    entered.set()
                    await finish.wait()
                    raise RuntimeError("模拟推理失败")

            task = asyncio.create_task(request())
            await entered.wait()
            expire(first)
            assert first.continuation_tickets
            if exit_kind == "cancel":
                task.cancel()
            else:
                finish.set()
            await asyncio.gather(task, return_exceptions=True)
            assert not first.continuation_tickets
            assert not harness.output.inference_boundary.frozen
            assert harness.output.speak_snapshot["queue_status"] == "empty"
            assert len(harness.tts.sessions) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_repeated_short_successors_keep_partition_and_delivery_monotonic():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        try:
            current = await harness.first()
            accumulated = FIRST_TEXT
            for tail in [SUFFIX_TEXT, "三。", "四。"]:
                await complete(harness, current)
                assert await revise(harness, runtime, tail) is current
                current = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
                accumulated += tail
                snapshot = harness.output.speak_snapshot
                assert snapshot["text"] == accumulated
                assert snapshot["sent_chars"] == len(accumulated)
                assert snapshot["_model_text_parts_valid"]
                assert model_state(harness)["continuation_mode"] == "rewrite_tail"
            await complete(harness, current)
            assert harness.output.delivered["text"] == accumulated
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_legitimate_repeated_suffix_is_kept_in_model_view_and_real_delivery():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        first_text = "屏幕显示的文字是甲乙丙丁戊己庚辛壬癸。"
        tail = first_text[-12:] + "这段需要再解释。"
        try:
            first = await harness.output.begin(harness.identity, harness.turn_id)
            await first.feed(first_text)
            await first.finish()
            await harness.wait_for_drain(first)
            await complete(harness, first)
            assert await revise(harness, runtime, tail) is first
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            snapshot = harness.output.speak_snapshot
            assert snapshot["text"] == first_text + tail
            assert snapshot["sent_chars"] == len(first_text + tail)
            assert snapshot["_model_text_parts_valid"]
            assert model_state(harness)["continuation_mode"] == "rewrite_tail"
            await complete(harness, second)
            assert harness.records[-1]["delivery"]["heard_text"] == tail
            assert harness.output.delivered["text"] == first_text + tail
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_same_turn_replacement_keeps_old_heard_prefix_after_delayed_settlement():
    async def scenario():
        harness = ContinuationHarness()
        _agent(harness)
        release_cancel = asyncio.Event()
        replacement_text = "纠正后的回答。"
        try:
            first = await harness.first()
            resolver = harness.output._delivery_resolvers[first.speech_id]
            resolver.add_provider_alignment([
                {"word": FIRST_TEXT[:2], "start_ms": 0, "end_ms": 10},
                {"word": FIRST_TEXT[2:], "start_ms": 70, "end_ms": 80},
            ])
            harness.render(20)
            original_cancel = first.tts_session.cancel

            async def delayed_cancel(**kwargs):
                await release_cancel.wait()
                await original_cancel(**kwargs)

            first.tts_session.cancel = delayed_cancel
            second = await harness.output.replace_current(harness.identity, harness.turn_id)
            assert harness.output.delivered["text"] == ""
            await second.feed(replacement_text)
            await second.finish()
            await harness.wait_for_drain(second)
            release_cancel.set()
            await asyncio.wait_for(asyncio.shield(first.task), HANDOFF_TIMEOUT_SECONDS)
            assert harness.records[0]["delivery"]["heard_text"] == FIRST_TEXT[:2]
            snapshot = harness.output.speak_snapshot
            assert snapshot["text"] == FIRST_TEXT[:2] + replacement_text
            assert snapshot["_model_text_parts_valid"]
            await complete(harness, second)
        finally:
            release_cancel.set()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("facts", [
    {"started": False, "buffered_ms": 0, "enqueued_ms": 80, "output_estimated_ms": 80},
    {"started": True, "buffered_ms": 0, "enqueued_ms": 0, "output_estimated_ms": 0},
    {"started": True, "buffered_ms": 0, "enqueued_ms": 80, "output_estimated_ms": 40},
])
def test_queue_drain_without_confirmed_device_output_does_not_open_grace_window(facts):
    async def scenario():
        harness = ContinuationHarness()
        _agent(harness)
        completed = []
        harness.runtime.playback_completed = lambda **kwargs: completed.append(kwargs)
        try:
            first = await harness.first()
            # 合成确实返回音频，但伪造的“软件队列已空”不是设备完成事实。
            first.workspace.timings.update(
                tts_text_sent=len(FIRST_TEXT), tts_audio_bytes=160, playback_output_complete=True,
            )
            first.snapshot = type("Snapshot", (), facts)()
            await harness.output._notify_playback_completed(first)
            assert first.continuation_deadline is None
            assert harness.output._continuation_handle is None
            assert completed == []
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_continuation_retains_backend_voice_and_inference_steps_until_transfer():
    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        local = harness.tts
        original_session = local.session
        frozen_voice = object()  # frozen_voice（原回答固定的音色快照）。
        selected_voice = [frozen_voice]
        pins, released = [], []
        local.voice_library = type("VoiceLibrary", (), {
            "pin": lambda _self, voice: (pins.append(voice) or (lambda: released.append(voice))),
        })()
        local.resolve_voice = lambda: selected_voice[0]

        def session(**kwargs):
            value = original_session()
            value.voice_snapshot = kwargs.get("voice_snapshot", selected_voice[0])
            value._inference_steps = kwargs.get("inference_steps", 12)
            return value

        local.session = session
        closed = []

        async def close_local():
            closed.append(True)

        local.close = close_local
        router = TtsBackendRouter(remote=None, local=local, backend="voxcpm1")
        harness.output.tts = router
        try:
            first = await harness.first()
            await complete(harness, first)
            assert router.active_local_sessions == 1
            assert pins == [frozen_voice] and released == []
            await router.switch("minimax")
            selected_voice[0] = object()
            assert closed == []
            assert await revise(harness, runtime, SUFFIX_TEXT) is first
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert second.tts_backend == "voxcpm1"
            assert second.tts_voice_snapshot is frozen_voice
            assert second.tts_inference_steps == 12
            assert router.active_local_sessions == 1
            assert pins == [frozen_voice, frozen_voice]
            assert released == [frozen_voice]
            await complete(harness, second)
        finally:
            await harness.close()
        assert router.active_local_sessions == 0
        assert released == [frozen_voice, frozen_voice]

    asyncio.run(scenario())


def test_screen_result_request_keeps_revision_authority_through_real_playback_end():
    """截图普通轮次在全锁定快照发起，返回时已过窗口，也能真实播出结论。"""
    from tests.test_screen_capture import packet

    async def scenario():
        harness = ContinuationHarness()
        runtime = _agent(harness)
        entered, release = asyncio.Event(), asyncio.Event()
        answer = "图片中左右两块区域显示的是同一个窗口。"

        class Provider:
            transport = "responses"  # transport（模型响应协议）。

            def stream_events(self, messages, **_kwargs):
                async def stream():
                    images = [part for message in messages
                              if isinstance(message.get("content"), list)
                              for part in message["content"] if part.get("type") == "image"]
                    assert len(images) == 2
                    state = runtime._last_model_speak_state
                    assert state["continuation_mode"] == "rewrite_tail"
                    assert state["text_parts"]["revisable_tail"] == ""
                    entered.set()
                    await release.wait()
                    yield {"type": "content", "text": json.dumps({
                        "speak": {"state": "revision", "emotion": "calm", "text": answer},
                        "actions": [],
                    }, ensure_ascii=False)}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        runtime.provider = Provider()
        runtime.agent_step_id = harness.identity[1]
        runtime.inputs = {harness.turn_id: {"text": "看一下当前画面", "final": True}}
        runtime._identity_turns[harness.identity] = harness.turn_id
        runtime.ledger.commit_input(harness.turn_id, "看一下当前画面")
        runtime._screen_pending = runtime.ledger.store_screen_capture(
            packet(), harness.identity[0], harness.turn_id,
        )
        step = None
        try:
            first = await harness.first()
            step = asyncio.create_task(runtime._step(harness.identity, {"SCREEN_CAPTURE_READY"}))
            await asyncio.wait_for(entered.wait(), HANDOFF_TIMEOUT_SECONDS)
            await complete(harness, first)
            expire(first)
            release.set()
            assert await asyncio.wait_for(step, HANDOFF_TIMEOUT_SECONDS) == "completed"
            second = await harness.wait_for_drain(timeout=HANDOFF_TIMEOUT_SECONDS)
            assert harness.tts.sessions[1].sent == [answer]
            await complete(harness, second)
            assert harness.output.delivered["text"] == FIRST_TEXT + answer
            assert not first.continuation_tickets
            assert not harness.output.inference_boundary.frozen
            assert runtime._screen_pending is None
        finally:
            if step is not None:
                step.cancel()
                await asyncio.gather(step, return_exceptions=True)
            runtime.ledger.screen_capture.close()
            await harness.close()

    asyncio.run(scenario())
