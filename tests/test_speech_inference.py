"""Speech inference（语音推理）边界：真实发送、模型快照和修订的组合回归。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.speech_inference import SpeechInferenceBoundary
from voice_terminal.controller.speech_admission import AdmissionState
from voice_terminal.tts.scheduling import TtsSegmentReceipt
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.shadow_agent.contracts import ShadowEvent
from tests.test_sentence_scheduling import _sentence_output_harness


FIRST = "第一段已经提交播放了。"  # FIRST（首个已锁定块，10 个有效字符）。
SECOND = "第二段准备继续播放了。"  # SECOND（请求前可补充锁定的块）。
OLD_TAIL = "第三段旧尾巴等待修改。"  # OLD_TAIL（尚未锁定的旧尾巴）。
NEW_TAIL = "第三段新的回答已经改好了。"  # NEW_TAIL（模型生成的新尾巴）。


async def until(predicate, timeout=2):
    """until（等待事件条件）：失败时有明确超时，不依赖长时间休眠。"""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def runtime_for(output, provider):
    """runtime_for（真实主模型运行时）：仅替换远端模型及工具目录。"""
    class Registry:
        def begin_turn(self, _turn):
            pass

        def action_definitions(self):
            return []

    async def emit(*_args, **_kwargs):
        pass

    conversation = ConversationRuntime()
    conversation.begin_turn("turn")
    runtime = AgentEpisodeRuntime(provider, Registry(), output, emit,
                                  conversation=conversation, ledger=SessionLedger())
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = "turn"
    runtime.agent_step_id = "request"
    runtime.inputs = {"turn": {"text": "修改最后一段", "final": True}}
    identity = (runtime.episode_id, "request")
    runtime._identity_turns[identity] = "turn"
    return runtime, identity


async def start_pipeline():
    """start_pipeline（真实发送流水线）：可控音频设备及合成回执。"""
    output, handle, session, player = _sentence_output_harness(min_effective_chars=10)
    handle.text = FIRST + SECOND + OLD_TAIL
    handle.finished = True

    async def deltas():
        yield handle.text
        yield None

    task = asyncio.create_task(output._play_stream(
        handle.workspace, handle.workspace.token, deltas(),
        speech_id=handle.speech_id, handle=handle,
    ))
    await until(lambda: session.completed_segments == 1)
    assert session.sent == [FIRST]
    return output, handle, session, player, task


def test_low_buffer_prefills_one_block_before_context_and_freezes_until_revision():
    async def scenario():
        output, handle, session, player, pipeline = await start_pipeline()
        player.played_ms = 200  # 剩余 2800 毫秒，尚未达到原调度的 2000 毫秒阈值。
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider:
            transport = "responses"  # transport（与产品一致的响应协议）。

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    assert session.sent == [FIRST, SECOND]
                    self.snapshot = output.speak_snapshot
                    entered.set()
                    await release.wait()
                    yield {"type": "content", "text": json.dumps({
                        "speak": {"state": "revision", "emotion": "calm", "text": NEW_TAIL},
                        "actions": [],
                    }, ensure_ascii=False)}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        provider = Provider()
        runtime, identity = runtime_for(output, provider)
        handle.identity = (identity[0], "old-request")
        step = asyncio.create_task(runtime._step(identity, {"USER_INPUT_REFINED"}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            locked = len(FIRST + SECOND)
            assert handle.sent_chars == locked
            assert provider.snapshot["sent_chars"] == locked
            assert runtime._last_model_speak_state["text_parts"]["revisable_tail"] == OLD_TAIL
            player.played_ms = 6000  # 模拟已锁定音频全部播放完。
            await asyncio.sleep(0.08)
            assert handle.sent_chars == locked
            assert session.sent == [FIRST, SECOND]
            release.set()
            assert await asyncio.wait_for(step, 2) == "completed"
            assert handle.text == FIRST + SECOND + NEW_TAIL
            assert not output.inference_boundary.frozen
            await until(lambda: len(session.sent) == 3)
            assert session.sent == [FIRST, SECOND, NEW_TAIL]
            await asyncio.wait_for(pipeline, 2)
        finally:
            step.cancel()
            pipeline.cancel()
            await asyncio.gather(step, pipeline, return_exceptions=True)

    asyncio.run(scenario())


def test_exact_three_seconds_needs_no_prefill_and_long_answer_resumes_after_wait():
    async def scenario():
        output, handle, session, player, pipeline = await start_pipeline()
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider:
            transport = "responses"  # transport（允许 wait 的空正文）。

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    assert session.sent == [FIRST]
                    entered.set()
                    await release.wait()
                    yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        runtime, identity = runtime_for(output, Provider())
        handle.identity = (identity[0], "old-request")
        step = asyncio.create_task(runtime._step(identity, {"USER_INPUT_REFINED"}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            player.played_ms = 9000
            await asyncio.sleep(0.08)
            assert session.sent == [FIRST]
            release.set()
            assert await asyncio.wait_for(step, 2) == "completed"
            await asyncio.wait_for(pipeline, 2)
            assert session.sent == [FIRST, SECOND, OLD_TAIL]
        finally:
            step.cancel()
            pipeline.cancel()
            await asyncio.gather(step, pipeline, return_exceptions=True)

    asyncio.run(scenario())


def test_request_waits_for_send_and_character_commit_to_finish():
    async def scenario():
        handle = SimpleNamespace(sent_chars=0, speech_id="speech")
        boundary = SpeechInferenceBoundary(SimpleNamespace(handle=handle, player=None), buffer_ms=0)
        sending, settle, frozen = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sender():
            async with boundary.submission(handle, lambda: True) as allowed:
                assert allowed
                sending.set()
                await settle.wait()
                handle.sent_chars = 10

        async def request():
            async with boundary.request(("episode", "request")):
                assert handle.sent_chars == 10
                frozen.set()

        send_task = asyncio.create_task(sender())
        await sending.wait()
        request_task = asyncio.create_task(request())
        await asyncio.sleep(0.02)
        assert not frozen.is_set()
        settle.set()
        await asyncio.wait_for(asyncio.gather(send_task, request_task), 1)
        assert not boundary.frozen

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_model_failure_and_cancellation_release_submission(failure):
    async def scenario():
        output = SimpleNamespace(handle=None, player=None)
        output.inference_boundary = SpeechInferenceBoundary(output)
        runtime, identity = runtime_for(output, None)
        entered = asyncio.Event()

        async def fail(*_args, **_kwargs):
            assert output.inference_boundary.frozen
            entered.set()
            if failure == "error":
                raise RuntimeError("模拟模型请求失败")
            await asyncio.Event().wait()

        runtime._step_with_speech_boundary = fail
        task = asyncio.create_task(runtime._step(identity, {"USER_INPUT_REFINED"}))
        await entered.wait()
        if failure == "cancel":
            task.cancel()
        with pytest.raises(RuntimeError if failure == "error" else asyncio.CancelledError):
            await task
        assert not output.inference_boundary.frozen
        async with output.inference_boundary.submission(None, lambda: True) as allowed:
            assert allowed

    asyncio.run(scenario())


def test_prefill_timeout_with_no_sender_does_not_leave_a_permit_or_freeze():
    async def scenario():
        output, handle, session, _player = _sentence_output_harness(min_effective_chars=10)
        handle.text = FIRST + SECOND
        handle.finished = True
        handle.tts_session = session
        handle.admission = SimpleNamespace(rewritable=True, sent_chars=len(FIRST), sentence_window=False)
        boundary = SpeechInferenceBoundary(output, prefill_timeout=0.01)
        async with boundary.request(("episode", "request")):
            assert boundary.frozen
            assert not boundary.prefilling(handle)
            assert handle.sent_chars == len(FIRST)
        assert not boundary.frozen

    asyncio.run(scenario())


def test_old_request_release_cannot_unlock_a_new_request():
    async def scenario():
        boundary = SpeechInferenceBoundary(SimpleNamespace(handle=None, player=None))
        outer = boundary.request(("episode", "same-request-id"))
        inner = boundary.request(("episode", "same-request-id"))
        await outer.__aenter__()
        await inner.__aenter__()
        await outer.__aexit__(None, None, None)
        assert boundary.frozen
        await inner.__aexit__(None, None, None)
        assert not boundary.frozen

    asyncio.run(scenario())


def test_empty_secondary_mailbox_does_not_freeze_or_request_a_model():
    async def scenario():
        output = SimpleNamespace(handle=None, player=None)
        output.inference_boundary = SpeechInferenceBoundary(output)
        @asynccontextmanager
        async def forbidden_request(_identity):
            pytest.fail("空二等信箱不应进入推理保护期")
            yield
        output.inference_boundary.request = forbidden_request
        runtime, identity = runtime_for(output, None)
        runtime._system_check_is_stale = lambda _context: False
        result = await runtime._step(identity, {"SYSTEM_CHECK"},
                                     dispatch_context={"prompt_kind": "secondary_mailbox"})
        assert result == "system_check_stale"
        assert not output.inference_boundary.frozen

    asyncio.run(scenario())


def test_secondary_retry_uses_its_valid_snapshot_when_the_mailbox_is_empty():
    async def scenario():
        output, handle, session, player, pipeline = await start_pipeline()

        class Provider:
            transport = "responses"  # transport（响应协议）。

            def stream_events(self, messages, **_kwargs):
                assert output.inference_boundary.frozen
                assert session.sent == [FIRST]
                assert "需要重试的原进度" in json.dumps(messages, ensure_ascii=False)

                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        runtime, identity = runtime_for(output, Provider())
        handle.identity = (identity[0], "old-request")
        runtime.ledger.set_shadow_runtime_state(available=True, active=True, activation_id="activation")
        runtime.ledger.commit_shadow_event(ShadowEvent(
            "progress", "progress", "需要重试的原进度", 1, "activation",
        ))
        captured = runtime._secondary_snapshot()
        runtime.ledger.consume_shadow_progress(captured)
        assert not runtime._secondary_snapshot()
        runtime._retry_secondary_by_turn["turn"] = captured
        runtime._system_check_is_stale = lambda _context: False
        try:
            assert await runtime._step(
                identity, {"LLM_RETRY"}, dispatch_context={"prompt_kind": "secondary_mailbox"},
            ) == "completed"
            assert not output.inference_boundary.frozen
            player.played_ms = 9000
            await asyncio.wait_for(pipeline, 2)
        finally:
            pipeline.cancel()
            await asyncio.gather(pipeline, return_exceptions=True)

    asyncio.run(scenario())


def test_stale_secondary_recovery_does_not_prepare_or_freeze_speech():
    async def scenario():
        output = SimpleNamespace(handle=None, player=None)
        output.inference_boundary = SpeechInferenceBoundary(output)
        @asynccontextmanager
        async def forbidden_request(_identity):
            pytest.fail("已过期的二等恢复请求不应额外锁定尾巴")
            yield
        output.inference_boundary.request = forbidden_request
        runtime, identity = runtime_for(output, None)
        runtime._system_check_is_stale = lambda _context: True
        result = await runtime._step(
            identity, {"LLM_RECOVERY"}, recovery=True,
            dispatch_context={"prompt_kind": "exception_recovery", "origin_prompt_kind": "secondary_mailbox"},
        )
        assert result == "system_check_stale"

    asyncio.run(scenario())


def test_popped_old_block_is_discarded_after_the_model_rewrites_it():
    async def scenario():
        output, handle, session, player, pipeline = await start_pipeline()
        entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_submission = output.inference_boundary.submission

        @asynccontextmanager
        async def observed_submission(current_handle, valid):
            waiting.set()
            async with original_submission(current_handle, valid) as allowed:
                yield allowed

        output.inference_boundary.submission = observed_submission

        class Provider:
            transport = "responses"

            def stream_events(self, _messages, **_kwargs):
                async def stream():
                    entered.set()
                    await release.wait()
                    yield {"type": "content", "text": json.dumps({
                        "speak": {"state": "revision", "emotion": "calm", "text": NEW_TAIL},
                        "actions": [],
                    }, ensure_ascii=False)}
                    yield {"type": "finish", "reason": "stop"}
                return stream()

        runtime, identity = runtime_for(output, Provider())
        handle.identity = (identity[0], "old-request")
        step = asyncio.create_task(runtime._step(identity, {"USER_INPUT_REFINED"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            player.played_ms = 3000
            await asyncio.wait_for(waiting.wait(), 1)
            assert list(handle.admission.staged) == [OLD_TAIL]
            assert session.sent == [FIRST]
            release.set()
            assert await asyncio.wait_for(step, 1) == "completed"
            await until(lambda: len(session.sent) == 2)
            assert session.sent == [FIRST, NEW_TAIL]
            await asyncio.wait_for(pipeline, 1)
        finally:
            step.cancel()
            pipeline.cancel()
            await asyncio.gather(step, pipeline, return_exceptions=True)

    asyncio.run(scenario())


def test_audio_arriving_during_preparation_cancels_unnecessary_prefill():
    async def scenario():
        output, handle, session, _player = _sentence_output_harness(min_effective_chars=10)
        handle.text = FIRST + SECOND
        handle.finished = True
        handle.tts_session = session
        handle.admission = AdmissionState(lead_ms=0)
        handle.admission.sent_chars = len(FIRST)
        entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        boundary = output.inference_boundary

        async def request():
            async with boundary.request(("episode", "request")):
                entered.set()
                await release.wait()

        async def sender():
            async with boundary.submission(handle, lambda: True) as allowed:
                assert allowed
                sent.set()

        request_task = asyncio.create_task(request())
        await until(lambda: boundary.prefilling(handle))
        session.received_bytes = 6000  # 等待期间已有 3000 毫秒音频返回。
        sender_task = asyncio.create_task(sender())
        await asyncio.wait_for(entered.wait(), 1)
        assert not boundary.prefilling(handle)
        assert not sent.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(request_task, sender_task), 1)
        assert sent.is_set()

    asyncio.run(scenario())


def test_prefill_preserves_current_and_prefetched_two_block_limit():
    async def scenario():
        output, handle, session, _player = _sentence_output_harness(min_effective_chars=10)
        handle.text = FIRST + SECOND + OLD_TAIL
        handle.finished = True
        handle.tts_session = session
        state = AdmissionState(lead_ms=0, sentence_window=True)
        state.commit_segment(FIRST, receipt_index=0)
        state.complete_segment(TtsSegmentReceipt(0, len(FIRST), 1000))
        state.commit_segment(SECOND, receipt_index=1)
        state.complete_segment(TtsSegmentReceipt(1, len(SECOND), 2000))
        state.push(OLD_TAIL)
        session.received_bytes = 2000
        handle.admission = state
        async with output.inference_boundary.request(("episode", "request")):
            assert not output.inference_boundary.prefilling(handle)
            assert state.sent_chars == len(FIRST + SECOND)
            assert list(state.staged) == [OLD_TAIL]

    asyncio.run(scenario())


def test_cancellation_during_prefill_preparation_releases_its_permit():
    async def scenario():
        output, handle, session, _player = _sentence_output_harness(min_effective_chars=10)
        handle.text = FIRST + SECOND
        handle.finished = True
        handle.tts_session = session
        handle.admission = AdmissionState(lead_ms=0)
        handle.admission.sent_chars = len(FIRST)
        boundary = output.inference_boundary

        async def request():
            async with boundary.request(("episode", "request")):
                pytest.fail("取消的准备操作不能开始模型请求")

        task = asyncio.create_task(request())
        await until(lambda: boundary.prefilling(handle))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not boundary.frozen
        assert not boundary.prefilling(handle)

    asyncio.run(scenario())


def test_stopped_transaction_does_not_wait_for_inference_release():
    async def scenario():
        boundary = SpeechInferenceBoundary(SimpleNamespace(handle=None, player=None))
        valid = True
        async with boundary.request(("episode", "request")):
            task = asyncio.create_task(boundary.wait_ready(None, lambda: valid))
            valid = False
            assert await asyncio.wait_for(task, 0.5) is False
            assert boundary.frozen

    asyncio.run(scenario())
