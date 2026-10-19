"""Contract tests for explicit speak revision, cancel-with-text and the
cross-transaction delivery frontier.

These cover the three defects that produced duplicate playback on 2026-09-26:
a prefix comparison that mis-read an extension as a divergence, a delivery
frontier that vanished when an appended successor became the queue head, and
an interruption result that never reached the authoritative state block.
"""

import asyncio
import json
from types import SimpleNamespace

from voice_terminal.agent.decision_envelope import (
    DecisionEnvelope,
    EnvelopeError,
    SpeechDecision,
    SpeechDecisionEnvelopeStream,
)
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime

# Real values from the 20:58 window on 2026-09-26: the first transaction spoke
# 71 characters, an appended successor added 13, and the next decision repeated
# the whole 86-character answer.
SPOKEN_FIRST = "刚才那句是我说得不清楚，抱歉。意思是：影子现在已经在运行中，正处理新一轮回合，但还没有新的已提交步骤。你是想让我再给它发一次唤醒，催它推进吗？"
SPOKEN_SUFFIX = "好，我这就给它发一次唤醒。"
MODEL_FULL = SPOKEN_FIRST + SPOKEN_SUFFIX + "号。"

# Real values from the 23:07 window: a barge-in interrupted the answer after
# the listener had heard the first clause only.
INTERRUPTED_FULL = "明白，是你本机的 VPN 在劫持 DNS。我让影子去查一下相关设置，看看有哪些可调项能让抓取绕过这个 fake-IP 判定。"
INTERRUPTED_HEARD = "明白，是你本机的 VPN 在劫持 DNS。"


class _Registry:
    def begin_turn(self, _turn_id):
        return None

    def action_definitions(self):
        return []


class _Handle:
    def __init__(self, speech_id, sink=None):
        self.speech_id = speech_id
        self.text = ""
        self._sink = sink

    async def feed(self, value):
        self.text += value
        if self._sink is not None:
            self._sink.append(value)

    async def finish(self):
        return None


class _Output:
    """Minimal EpisodeSpeechOutput substitute with the output primitives."""

    status = "IDLE"
    active = False
    last_completed_speech = None

    def __init__(self, snapshot):
        self.speak_snapshot = snapshot
        self.calls = []
        self.feeds = []
        self.cancelled = []
        self.handles = {
            "replace_current": _Handle("replacement", self.feeds),
            "append_successor": _Handle("successor", self.feeds),
            "begin": _Handle("fresh", self.feeds),
        }

    async def rewrite_unsent(self, identity, turn_id, *, speech_id, text, **confirmation):
        self.calls.append(("rewrite", speech_id, text))
        return None if self.rewrite_unavailable else _Handle(speech_id)

    rewrite_unavailable = False

    async def append_successor(self, identity, turn_id, *, baseline_speech_id,
                               baseline_generation, suffix, emotion="calm"):
        self.calls.append(("append", baseline_speech_id, suffix))
        if self.append_unavailable:
            return None
        handle = self.handles["append_successor"]
        await handle.feed(suffix)
        return handle

    append_unavailable = False

    async def replace_current(self, identity, turn_id, *, emotion="calm"):
        self.calls.append(("replace", None, None))
        return self.handles["replace_current"]

    async def begin(self, identity, turn_id, *, emotion="calm", **kwargs):
        self.calls.append(("begin", None, None))
        return self.handles["begin"]

    def cancel_all(self, reason):
        self.cancelled.append(reason)


def _runtime(output, turn_id="turn-a"):
    async def emit(*_args, **_kwargs):
        return None

    conversation = ConversationRuntime()
    conversation.begin_turn(turn_id)
    runtime = AgentEpisodeRuntime(None, _Registry(), output, emit,
                                  conversation=conversation, ledger=SessionLedger())
    runtime.episode_id = conversation.conversation_id
    runtime.last_turn_id = turn_id
    return runtime, (conversation.conversation_id, "request")


# --------------------------------------------------------------------------
# A. revision semantics
# --------------------------------------------------------------------------

def test_revision_carries_only_the_new_tail_and_is_stitched_locally():
    """``revision.text`` is the tail; the locked prefix never travels again."""

    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "output_generation": 1, "text": "第一句已经送出，",
            "sent_chars": len("第一句已经送出，"), "rewritable": True,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("revision", "calm", "改口后的尾巴。"), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == [("rewrite", "live", "改口后的尾巴。")]
        assert runtime.current_action == "speak"

    asyncio.run(scenario())


def test_revision_window_closed_is_rejected_without_fallback():
    """关闭的修订窗口拒绝请求，不能追加、替换或新建。"""

    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "output_generation": 4, "text": "第一句已经送出，",
            "sent_chars": len("第一句已经送出，"), "rewritable": False,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("revision", "calm", "补充一句。"), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == []
        assert runtime.minor_protocol_feedback[-1]["reason"] == "revision_window_closed"
        assert not any(call[0] == "replace" for call in output.calls)
        assert output.feeds == []

    asyncio.run(scenario())


def test_revision_without_a_live_transaction_is_rejected():
    """没有目标事务时修订不能变成新播报。"""

    async def scenario():
        output = _Output({"queue_status": "empty", "text_only_queue": True})
        runtime, identity = _runtime(output)

        envelope = DecisionEnvelope(SpeechDecision("revision", "calm", "新的一句。"), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == []
        assert output.feeds == []
        assert runtime.minor_protocol_feedback[-1]["reason"] == "no_owned_transaction"

    asyncio.run(scenario())


def test_revision_repeating_the_locked_tail_is_a_visible_noop():
    """Nothing is replayed and the model is told locally, not silently."""

    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "text": "尾巴已经锁定。", "sent_chars": len("尾巴已经锁定。"),
            "rewritable": True,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("revision", "calm", "已经锁定。"), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == []
        assert output.feeds == []
        assert runtime.current_action == "speak"
        assert runtime.minor_protocol_feedback
        assert runtime.minor_protocol_feedback[-1]["applied"] == "noop_tail_already_locked"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# B. cancel semantics
# --------------------------------------------------------------------------

def test_cancel_with_text_stops_and_replaces_immediately():
    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "text": "正在播的内容", "sent_chars": 7, "rewritable": False,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]
        replacement = "我刚才听错了，重新说一遍。"

        envelope = DecisionEnvelope(SpeechDecision("cancel", "calm", replacement), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == [("replace", None, None)]
        assert output.feeds == [replacement]
        assert output.cancelled == []
        assert runtime.current_action == "speak"

    asyncio.run(scenario())


def test_cancel_without_text_still_stops_only():
    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "text": "正在播的内容", "sent_chars": 7, "rewritable": False,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("cancel", "calm", ""), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.cancelled == ["cancelled"]
        assert output.calls == []
        assert output.feeds == []
        assert runtime.current_action == "wait"

    asyncio.run(scenario())


def test_envelope_accepts_revision_and_cancel_text_but_rejects_empty_revision():
    parser = SpeechDecisionEnvelopeStream(allow_empty_control_text=True)
    parser.feed('{"speak":{"state":"revision","emotion":"calm","text":"新尾巴。"},"actions":[]}')
    assert parser.finish().speak == SpeechDecision("revision", "calm", "新尾巴。")

    cancel = SpeechDecisionEnvelopeStream()
    cancel.feed('{"speak":{"state":"cancel","emotion":"calm","text":"改口。"},"actions":[]}')
    assert cancel.finish().speak.text == "改口。"

    empty_revision = SpeechDecisionEnvelopeStream()
    empty_revision.feed('{"speak":{"state":"revision","emotion":"calm","text":""},"actions":[]}')
    try:
        empty_revision.finish()
    except EnvelopeError as exc:
        assert "nonempty text" in str(exc)
    else:
        raise AssertionError("revision must require a non-empty tail")


# --------------------------------------------------------------------------
# C. delivery frontier
# --------------------------------------------------------------------------

def test_snapshot_keeps_the_frontier_when_a_successor_becomes_the_head():
    """The 71 -> 13 collapse: the accepted text may never shrink."""

    output = object.__new__(EpisodeSpeechOutput)
    output._pending = []
    output.last_delivery = None
    output.handle = SimpleNamespace(
        reason=None,
        task=SimpleNamespace(done=lambda: False),
        workspace=SimpleNamespace(playback_started=True, generation=3, playback_key="key:3"),
        state="READY",
        text=SPOKEN_SUFFIX,
        speech_id="successor",
        identity=("conversation", "request"),
        turn_id="turn-a",
        emotion_requested="calm",
        admission=None,
        sent_chars=len(SPOKEN_SUFFIX),
        rewritable=False,
        tts_session=None,
    )
    output.delivered = {"turn_id": "turn-a", "text": SPOKEN_FIRST}

    snapshot = output.speak_snapshot

    # ``append_successor`` feeds a successor only its own suffix, so the
    # frontier has to be restored in front of it for the model to see the
    # answer it is actually continuing.
    assert snapshot["text"] == SPOKEN_FIRST + SPOKEN_SUFFIX
    assert snapshot["sent_chars"] == len(SPOKEN_FIRST) + len(SPOKEN_SUFFIX)
    parts = snapshot["_model_text_parts"]
    assert "".join(parts.values()) == snapshot["text"]
    # 后继也已全部提交，不能仅因前一段更长就把已锁定后继误报为可改尾巴。
    assert parts["committed_prefix"] == SPOKEN_FIRST + SPOKEN_SUFFIX
    assert parts["revisable_tail"] == ""
    assert snapshot["_model_text_parts_valid"] is True


def test_same_turn_speak_extension_only_feeds_what_was_not_accepted():
    """A same-turn decision repeating accepted text must not replay it."""

    async def scenario():
        output = _Output({
            "queue_status": "empty", "text_only_queue": True,
            "text": "", "delivered_text": SPOKEN_FIRST, "delivered_turn_id": "turn-a",
            "delivered_resumable": True,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", MODEL_FULL), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        # Only the genuinely new remainder reaches a transaction; the 71
        # accepted characters are never synthesised again.
        assert output.calls == [("begin", None, None)]
        assert output.feeds == [SPOKEN_SUFFIX + "号。"]

    asyncio.run(scenario())


def test_queue_empty_snapshot_reports_what_the_listener_heard():
    """After a barge-in the authoritative block must locate the continuation."""

    output = object.__new__(EpisodeSpeechOutput)
    output._pending = []
    output.handle = None
    output.delivered = {"turn_id": "old-turn", "text": INTERRUPTED_FULL}
    output.last_delivery = {
        "turn_id": "old-turn",
        "text": INTERRUPTED_HEARD,
        "heard_text": INTERRUPTED_HEARD,
        "status": "partial",
        "termination": "interrupted",
    }

    snapshot = output.speak_snapshot
    assert snapshot["queue_status"] == "empty"
    assert snapshot["delivery"] == {
        "heard_text": INTERRUPTED_HEARD, "status": "partial", "termination": "interrupted",
    }
    # The full answer stays available as a repeat baseline, but it is flagged
    # as not resumable because the user's barge-in ended that act on purpose.
    assert snapshot["delivered_text"] == INTERRUPTED_FULL
    assert snapshot["delivered_resumable"] is False

    state = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="conversation", turn_id="new-turn",
    )
    assert state["state"] == "empty"
    assert state["owner_scope"] == "none"
    assert state["continuation_mode"] == "none"
    assert state["delivery"]["heard_text"] == INTERRUPTED_HEARD
    # No runtime identifier may leak into the model-visible state.
    encoded = json.dumps(state, ensure_ascii=False)
    assert "speech_id" not in encoded and "turn_id" not in encoded


def test_interrupted_delivery_becomes_a_heard_prefix_baseline():
    """A partial delivery is usable through its heard text, never its full text."""

    output = SimpleNamespace(last_completed_speech={
        "speech_id": "interrupted", "conversation_id": "conversation",
        "turn_id": "old-turn", "output_generation": 1,
        "text": INTERRUPTED_FULL, "heard_text": INTERRUPTED_HEARD,
        "delivery_status": "partial", "termination": "interrupted",
        "status": "interrupted",
    })

    async def emit(*_args, **_kwargs):
        return None

    conversation = ConversationRuntime()
    conversation.begin_turn("new-turn")
    runtime = AgentEpisodeRuntime(None, _Registry(), output, emit,
                                  conversation=conversation, ledger=SessionLedger())
    identity = (conversation.conversation_id, "request")
    output.last_completed_speech["conversation_id"] = identity[0]

    guard = runtime._completed_speech_guard(identity, "new-turn")
    assert guard is not None
    assert guard["source"] == "heard_prefix_guard"
    assert guard["text"] == INTERRUPTED_HEARD

    # A partial delivery with nothing audible carries no baseline at all.
    output.last_completed_speech["heard_text"] = ""
    assert runtime._completed_speech_guard(identity, "new-turn") is None


def test_next_turn_repeating_an_interrupted_answer_only_feeds_the_unheard_tail():
    """Window B: the replayed answer must resume where the barge-in cut it."""

    async def scenario():
        output = _Output({
            "queue_status": "empty", "text_only_queue": True,
            "delivery": {"heard_text": INTERRUPTED_HEARD, "status": "partial",
                         "termination": "interrupted"},
        })
        output.last_completed_speech = {
            "speech_id": "interrupted", "conversation_id": "conversation",
            "turn_id": "old-turn", "output_generation": 1,
            "text": INTERRUPTED_FULL, "heard_text": INTERRUPTED_HEARD,
            "delivery_status": "partial", "termination": "interrupted",
            "status": "interrupted",
        }
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]
        output.last_completed_speech["conversation_id"] = identity[0]

        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", INTERRUPTED_FULL), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)

        assert output.calls == [("begin", None, None)]
        assert output.feeds == [INTERRUPTED_FULL[len(INTERRUPTED_HEARD):]]

    asyncio.run(scenario())


def test_recording_a_delivery_accumulates_the_frontier_within_one_turn():
    output = object.__new__(EpisodeSpeechOutput)
    output.delivered = {"turn_id": None, "text": ""}
    output.last_delivery = None

    output.record_delivery({
        "turn_id": "turn-a", "status": "completed", "text": SPOKEN_FIRST,
        "delivery": {"status": "full", "termination": "completed",
                     "heard_text": SPOKEN_FIRST},
    })
    assert output.delivered == {"turn_id": "turn-a", "text": SPOKEN_FIRST}

    # 提前合成但未听到的事务不能推进送达前缀。
    output.record_delivery({
        "turn_id": "turn-a", "status": "replaced", "text": SPOKEN_SUFFIX + "号。",
        "tts_text_chars": len(SPOKEN_SUFFIX),
        "delivery": {"status": "none", "termination": "replaced", "heard_text": ""},
    })
    assert output.delivered["text"] == SPOKEN_FIRST
    output.record_delivery({"turn_id": "turn-a", "status": "interrupted", "text": SPOKEN_SUFFIX,
                            "delivery": {"status": "partial", "heard_text": SPOKEN_SUFFIX[:3]}})
    assert output.delivered["text"] == SPOKEN_FIRST + SPOKEN_SUFFIX[:3]

    # A new turn starts a new frontier instead of extending the old one.
    output.record_delivery({
        "turn_id": "turn-b", "status": "completed", "text": "新一轮。",
        "delivery": {"status": "full", "termination": "completed", "heard_text": "新一轮。"},
    })
    assert output.delivered == {"turn_id": "turn-b", "text": "新一轮。"}
    assert output.last_delivery["heard_text"] == "新一轮。"


def test_minor_feedback_rides_the_request_tail_and_keeps_the_cache_envelope():
    """The degradation notice must not disturb the cached lanes or the order."""

    async def scenario():
        from voice_terminal.session.context import FINAL_OUTPUT_CONSTRAINT
        from voice_terminal.session.context_frames import ContextFrame, ContextLane

        output = _Output({"queue_status": "empty", "text_only_queue": True})
        runtime, identity = _runtime(output)
        runtime.inputs = {"turn-a": {"text": "继续", "final": True}}
        runtime._note_identical_speak("active_same_turn")

        messages = runtime.messages({"USER_SEGMENT_UPDATED"}, current_turn_id="turn-a")

        # The final JSON emphasis stays last; the v2 snapshot and task retain
        # their fixed order while unrelated dynamic frames stay independent.
        assert messages[-1]["content"] == FINAL_OUTPUT_CONSTRAINT
        assert messages[-1]["context_kind"] == "output_constraint"
        assert messages[-2]["context_kind"] == "speech_queue_task"
        packet = json.loads(next(
            item["content"] for item in messages
            if item.get("context_kind") == "voice_request_context"
        ))
        assert packet["type"] == "voice_request_context"
        assert packet["version"] == 2
        dynamic_kinds = [item.get("context_kind") for item in messages
                         if item.get("context_lane") == "request_tail"]
        assert dynamic_kinds.count("minor_protocol_feedback") == 1

        history = [item for item in messages
                   if ContextFrame.lane_of(item) == ContextLane.HISTORY]
        assert not any("minor_protocol_feedback" in json.dumps(item, ensure_ascii=False)
                       for item in history)

    asyncio.run(scenario())


def test_revision_rejects_changed_request_boundary_without_any_fallback():
    async def scenario():
        for changed in (
            {"sent_chars": 5},
            {"text": "前缀新尾巴"},
            {"text_revision": 1},
            {"speech_id": "another"},
            {"output_generation": 2},
            {"rewritable": False},
        ):
            output = _Output({
                "queue_status": "speaking", "speech_id": "live",
                "conversation_id": "conversation", "turn_id": "turn-a",
                "output_generation": 1, "text": "前缀旧尾巴",
                "sent_chars": 2, "rewritable": True, "text_revision": 0,
            })
            runtime, identity = _runtime(output)
            output.speak_snapshot["conversation_id"] = identity[0]
            frozen = dict(output.speak_snapshot)
            output.speak_snapshot.update(changed)
            result = await runtime._apply_revision_tail(
                identity, "turn-a", frozen, "新尾巴", emotion="calm")
            assert result is None
            assert output.calls == [] and output.feeds == []
            assert runtime.minor_protocol_feedback[-1]["applied"] == "rejected_boundary_changed"
    asyncio.run(scenario())


def test_revision_accepts_a_different_turn_but_rejects_failed_final_confirmation():
    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "output_generation": 1, "text": "前缀旧尾巴",
            "sent_chars": 2, "rewritable": True, "text_revision": 0,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]
        envelope = DecisionEnvelope(SpeechDecision("revision", "calm", "新尾巴"), ())
        await runtime._apply_decision_v06(identity, "turn-b", envelope)
        assert output.calls == [("rewrite", "live", "新尾巴")]
        assert runtime.current_action == "speak"
        output.calls.clear()
        output.rewrite_unavailable = True
        await runtime._apply_decision_v06(identity, "turn-a", envelope)
        assert output.calls == [("rewrite", "live", "新尾巴")]
        assert runtime.current_action == "wait"
        assert output.feeds == []
    asyncio.run(scenario())


def test_previous_turn_revision_permission_depends_on_transaction_not_turn():
    snapshot = {
        "queue_status": "speaking", "speech_id": "live",
        "conversation_id": "conversation", "turn_id": "turn-a",
        "text": "已锁定旧尾巴", "sent_chars": 3, "rewritable": True,
    }
    state = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="conversation", turn_id="turn-b",
    )
    assert state["owner_scope"] == "previous_turn"
    assert state["continuation_mode"] == "rewrite_tail"
    assert state["text_parts"]["revisable_tail"] == "旧尾巴"
    other = AgentEpisodeRuntime._model_speak_state(
        snapshot, conversation_id="other", turn_id="turn-b",
    )
    assert other["continuation_mode"] == "none"


def test_cross_turn_revision_still_rejects_a_different_conversation():
    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "other", "turn_id": "turn-a",
            "text": "旧尾巴", "sent_chars": 0, "rewritable": True,
        })
        runtime, identity = _runtime(output)
        result = await runtime._apply_revision_tail(
            identity, "turn-b", dict(output.speak_snapshot), "新尾巴", emotion="calm",
        )
        assert result is None
        assert output.calls == []
        assert runtime.minor_protocol_feedback[-1]["reason"] == "no_owned_transaction"
    asyncio.run(scenario())


def test_model_request_freezes_revision_boundary_before_frontier_advances():
    async def scenario():
        output = _Output({
            "queue_status": "speaking", "speech_id": "live",
            "conversation_id": "conversation", "turn_id": "turn-a",
            "output_generation": 1, "text": "前缀旧尾巴",
            "sent_chars": 2, "rewritable": True, "text_revision": 0,
        })
        runtime, identity = _runtime(output)
        output.speak_snapshot["conversation_id"] = identity[0]
        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    output.speak_snapshot["sent_chars"] = 5
                    yield {"type": "content", "text": '{"speak":{"state":"revision","emotion":"calm","text":"新尾巴"},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        runtime.provider = Provider()
        runtime.inputs = {"turn-a": {"text": "继续", "final": True}}
        runtime.agent_step_id = identity[1]
        runtime._identity_turns[identity] = "turn-a"
        assert await runtime._step(identity, {"USER_INPUT_REFINED"}) == "completed"
        assert runtime._last_model_speak_snapshot["sent_chars"] == 2
        assert output.calls == []
        assert runtime.minor_protocol_feedback[-1]["reason"] == "request_boundary_changed"
    asyncio.run(scenario())


def test_busy_speak_is_not_deferred_by_passive_quiet():
    async def scenario():
        output = _Output({"queue_status": "speaking", "text": "正在播放"})
        runtime, identity = _runtime(output)
        runtime.should_defer_speech = lambda: True
        deferred = []
        runtime.defer_speech = lambda *args: deferred.append(args)
        envelope = DecisionEnvelope(SpeechDecision("speak", "calm", "不能追加"), ())
        await runtime._apply_decision_v06(identity, "turn-a", envelope)
        assert deferred == [] and output.calls == []
    asyncio.run(scenario())


def test_ignored_busy_speak_does_not_block_validated_actions():
    async def scenario():
        output = _Output({"queue_status": "speaking", "speech_id": "live", "text": "正在播放"})
        runtime, identity = _runtime(output)
        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","emotion":"calm","text":"不能追加"},"actions":[{"name":"request_shadow","arguments":{"request":true}}]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        dispatched = []
        async def dispatch(_identity, _turn, envelope):
            dispatched.extend(envelope.actions)
        runtime.provider = Provider()
        runtime._dispatch_actions_v06 = dispatch
        runtime.inputs = {"turn-a": {"text": "继续查资料", "final": True}}
        runtime.agent_step_id = identity[1]
        runtime._identity_turns[identity] = "turn-a"
        assert await runtime._step(identity, {"USER_INPUT_FINALIZED"}) == "completed"
        assert output.calls == [] and output.feeds == []
        assert [action.name for action in dispatched] == ["request_shadow"]
    asyncio.run(scenario())


def test_passive_quiet_mid_stream_keeps_the_entire_unplayed_proposal():
    async def scenario():
        output = _Output({"queue_status": "empty"})
        runtime, identity = _runtime(output)
        handle = output.handles["begin"]
        original_feed = handle.feed
        async def feed(value):
            await original_feed(value)
            handle.reason = "passive_quiet"
        handle.feed = feed
        output.deferred_remainder = lambda _handle, text: (text, "")
        class Provider:
            def stream_events(self, _messages, *, json_mode=False):
                async def stream():
                    yield {"type": "content", "text": '{"speak":{"state":"speak","emotion":"calm","text":"完整'}
                    yield {"type": "content", "text": '回答尾巴"},"actions":[]}'}
                    yield {"type": "finish", "reason": "stop"}
                return stream()
        deferred = []
        runtime.provider = Provider()
        runtime.defer_speech = lambda _source, text, **_kwargs: deferred.append(text)
        runtime.inputs = {"turn-a": {"text": "继续", "final": True}}
        runtime.agent_step_id = identity[1]
        runtime._identity_turns[identity] = "turn-a"
        assert await runtime._step(identity, {"USER_INPUT_FINALIZED"}) == "completed"
        assert deferred == ["完整回答尾巴"]
    asyncio.run(scenario())
