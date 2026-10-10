"""诊断日志覆盖：修订和最终播报结算。

测试实际调用 AgentEpisodeRuntime._apply_revision_tail（播报尾巴修订应用）及
EpisodeSpeechOutput._run（最终播报结算路径），检查关联标识和长度是否可观测，
并确认日志不依赖或泄漏播报正文。

诊断字段说明：episode_id（会话阶段标识）、request_id（模型请求标识）、
turn_id（用户轮次标识）、speech_id（播报标识）、
context_locked_chars（请求锁定码点数）、apply_locked_chars（应用锁定码点数）、
new_tail_chars（提议新尾巴码点数）、text_chars（完整正文码点数）、
heard_chars（确认已听码点数）、uncertain_chars（不确定码点数）、
unheard_chars（确认未听码点数）、termination（终止原因）、
precision（传达判断精度）。
"""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.audio.player import PlaybackSnapshot
from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


class _Registry:
    def begin_turn(self, _turn_id):
        return None

    def action_definitions(self):
        return []


class _RevisionOutput:
    """Small output boundary used to drive the real tail-application method."""

    active = True
    status = "SPEAKING"
    active_snapshot = None
    queue_snapshot = []

    def __init__(self, snapshot, *, apply_locked_chars=None):
        self.speak_snapshot = dict(snapshot)
        self.apply_locked_chars = apply_locked_chars
        self.rewrite_calls = []

    async def rewrite_unsent(self, identity, turn_id, *, speech_id, text, **confirmation):
        self.rewrite_calls.append({
            "identity": identity,
            "turn_id": turn_id,
            "speech_id": speech_id,
            "text": text,
            "confirmation": confirmation,
        })
        applied = self.apply_locked_chars
        if applied is None:
            applied = confirmation["expected_sent_chars"]
        return SimpleNamespace(speech_id=speech_id, sent_chars=applied)


def _revision_runtime(snapshot, *, current_snapshot=None, apply_locked_chars=None):
    emitted = []

    async def emit(kind, payload):
        emitted.append((kind, payload))

    conversation = ConversationRuntime()
    conversation.begin_turn("turn-diagnostic")
    snapshot["conversation_id"] = conversation.conversation_id
    output = _RevisionOutput(snapshot, apply_locked_chars=apply_locked_chars)
    if current_snapshot is not None:
        output.speak_snapshot = dict(
            current_snapshot, conversation_id=conversation.conversation_id,
        )
    runtime = AgentEpisodeRuntime(
        None, _Registry(), output, emit,
        conversation=conversation,
        ledger=SessionLedger(),
    )
    runtime.episode_id = conversation.conversation_id
    runtime.agent_step_id = "request-diagnostic"
    runtime.last_turn_id = "turn-diagnostic"
    identity = (runtime.episode_id, runtime.agent_step_id)
    runtime._identity_turns[identity] = runtime.last_turn_id
    return runtime, output, emitted, identity


def _revision_snapshot(*, text, locked_chars, speech_id="speech-diagnostic"):
    return {
        "queue_status": "speaking",
        "speech_id": speech_id,
        "conversation_id": "conversation-diagnostic",
        "turn_id": "turn-diagnostic",
        "output_generation": 3,
        "text": text,
        "sent_chars": locked_chars,
        "text_revision": 2,
        "rewritable": True,
    }


def _trace_for(emitted, kind):
    return next(
        payload for event_kind, payload in emitted
        if event_kind == "agent.trace" and payload.get("kind") == kind
    )


def _assert_revision_diagnostics(caplog, emitted, *, kind, identity, turn_id,
                                 speech_id, context_locked_chars, apply_locked_chars,
                                 new_tail_chars, secret_text):
    trace = _trace_for(emitted, kind)
    assert trace["conversation_id"] == identity[0]
    assert trace["request_id"] == identity[1]
    assert trace["turn_id"] == turn_id
    assert trace["speech_id"] == speech_id
    assert trace["context_locked_chars"] == context_locked_chars
    assert trace["apply_locked_chars"] == apply_locked_chars
    assert trace["new_tail_chars"] == new_tail_chars
    assert secret_text not in repr(trace)

    line = next(
        record.getMessage() for record in caplog.records
        if record.name == "voice_terminal.agent.episode"
        and "agent.speech_revision" in record.getMessage()
        and f"kind={kind}" in record.getMessage()
        and f"speech_id={speech_id}" in record.getMessage()
    )
    assert f"episode_id={identity[0]}" in line
    assert f"request_id={identity[1]}" in line
    assert f"turn={turn_id}" in line
    assert f"context_locked_chars={context_locked_chars}" in line
    assert f"apply_locked_chars={apply_locked_chars}" in line
    assert f"new_tail_chars={new_tail_chars}" in line
    assert secret_text not in line


def test_revision_diagnostics_cover_accept_reject_and_noop_without_logging_text(caplog):
    async def scenario():
        with caplog.at_level(logging.INFO, logger="voice_terminal.agent.episode"):
            # accepted（已受理）：context_locked_chars（请求锁定码点数）与
            # apply_locked_chars（应用锁定码点数）边界一致。
            active_text = "已提交前缀SECRET_LOCKED_BODY尚未发送"
            locked_chars = len("已提交前缀SECRET_LOCKED_BODY")
            accepted = _revision_snapshot(text=active_text, locked_chars=locked_chars)
            runtime, output, emitted, identity = _revision_runtime(accepted)
            accepted_tail = "SECRET_NEW_TAIL_BODY"
            rewritten = await runtime._apply_revision_tail(
                identity, "turn-diagnostic", accepted, accepted_tail, emotion="calm",
            )
            assert rewritten.speech_id == accepted["speech_id"]
            assert len(output.rewrite_calls) == 1
            _assert_revision_diagnostics(
                caplog, emitted, kind="SPEECH_REVISED", identity=identity,
                turn_id="turn-diagnostic", speech_id=accepted["speech_id"],
                context_locked_chars=locked_chars, apply_locked_chars=locked_chars,
                new_tail_chars=len(accepted_tail), secret_text="SECRET_",
            )

            # rejected（已拒绝）：context_locked_chars（请求锁定码点数）为 4，
            # apply_locked_chars（应用锁定码点数）已推进到 6。
            rejected_text = "LOCKED_SECRET_PREFIX后续"
            request_locked = len("LOCKED_SECRET_PREFIX")
            frozen = _revision_snapshot(text=rejected_text, locked_chars=request_locked,
                                        speech_id="speech-rejected")
            advanced = dict(frozen, sent_chars=request_locked + 2)
            runtime, output, emitted, identity = _revision_runtime(
                frozen, current_snapshot=advanced,
            )
            rejected_tail = "SECRET_REJECTED_TAIL"
            result = await runtime._apply_revision_tail(
                identity, "turn-diagnostic", frozen, rejected_tail, emotion="calm",
            )
            assert result is None
            assert output.rewrite_calls == []
            _assert_revision_diagnostics(
                caplog, emitted, kind="SPEECH_REVISION_REJECTED", identity=identity,
                turn_id="turn-diagnostic", speech_id=frozen["speech_id"],
                context_locked_chars=request_locked,
                apply_locked_chars=request_locked + 2,
                new_tail_chars=len(rejected_tail), secret_text="SECRET_",
            )

            # noop（无操作）：提议内容已包含在锁定尾部，不触发改写。
            locked_text = "前缀SECRET_ALREADY_LOCKED_TAIL"
            no_op_locked = len(locked_text)
            no_op = _revision_snapshot(text=locked_text, locked_chars=no_op_locked,
                                       speech_id="speech-noop")
            runtime, output, emitted, identity = _revision_runtime(no_op)
            already_locked_tail = "SECRET_ALREADY_LOCKED_TAIL"
            result = await runtime._apply_revision_tail(
                identity, "turn-diagnostic", no_op, already_locked_tail, emotion="calm",
            )
            assert result == "noop"
            assert output.rewrite_calls == []
            _assert_revision_diagnostics(
                caplog, emitted, kind="SPEECH_REVISION_NOOP", identity=identity,
                turn_id="turn-diagnostic", speech_id=no_op["speech_id"],
                context_locked_chars=no_op_locked, apply_locked_chars=no_op_locked,
                new_tail_chars=len(already_locked_tail), secret_text="SECRET_",
            )

    asyncio.run(scenario())


class _SettlementPlayer:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def stop_and_snapshot(self, _key):
        return self.snapshot


async def _settle_output_case(case):
    text = case["text"]
    expected = case["expected"]
    snapshot = PlaybackSnapshot(
        "playback-diagnostic", enqueued_ms=300, played_ms=case["played_ms"],
        buffered_ms=300 - case["played_ms"],
        output_estimated_ms=case["played_ms"], started=True,
        captured_at=time.monotonic(),
    )
    controller = SimpleNamespace(
        tts=SimpleNamespace(
            emotion_status="applied", emotion_transport="voice_setting",
            applied_emotion="calm",
        ),
        player=_SettlementPlayer(snapshot),
        options=SimpleNamespace(output_limit=20),
        bridge_options={},
        _turn_timings={},
        state=SimpleNamespace(latency={"values": {}}, current={"tts_status": "FINISHED"}),
        workspace=None,
        _add_playback_delivery_fields=lambda delivery, _snapshot: delivery,
    )
    output = EpisodeSpeechOutput(controller)
    captured = {}

    async def emit(*_args):
        return None

    async def trace(kind, **payload):
        captured.setdefault("traces", []).append((kind, payload))

    async def output_completed(record):
        captured["record"] = record

    output.runtime = SimpleNamespace(
        gate=lambda _identity: None,
        emit=emit,
        log=trace,
        closed=False,
        request_started_at=None,
        input_finished_at={},
        output_completed=output_completed,
    )
    handle = SpeechHandle(
        output, ("episode-settlement", f"request-{case['name']}"),
        f"turn-{case['name']}", 0, "calm",
    )
    handle.text = text
    handle.tts_backend = "diagnostic-test"
    handle.task = asyncio.current_task()
    output.handle = handle
    output._handles_by_token[handle.workspace.token] = handle
    resolver = output._delivery_resolver_for(handle.workspace, speech_id=handle.speech_id)
    if case.get("alignment"):
        resolver.add_provider_alignment(case["alignment"])

    async def fake_play(ws, *_args, **_kwargs):
        ws.playback_started = True
        ws.timings.update({
            "tts_text_sent": 1,
            "tts_audio_bytes": 32000,
            "playback_output_complete": case["playback_complete"],
        })
        output.state.current["tts_status"] = "FINISHED"
        if case.get("termination"):
            handle.reason = case["termination"]
        # 跳过真实流与播放器循环，但 _run 仍执行实际最终传达解析、结算记录构造和日志路径。
        output.handle = None

    output._play_stream = fake_play
    await output._run(handle)
    return captured


@pytest.mark.parametrize(
    "case",
    [
        {
            "name": "full",
            "text": "完整回答",
            "played_ms": 300,
            "playback_complete": True,
            "expected": {
                "status": "full", "termination": "completed",
                "precision": "playback_complete", "text_chars": 4,
                "heard_chars": 4, "uncertain_chars": 0, "unheard_chars": 0,
            },
        },
        {
            "name": "partial",
            "text": "已听。未听。",
            "played_ms": 150,
            "playback_complete": False,
            "termination": "interrupted",
            "alignment": {"words": [
                {"word": "已听。", "start_ms": 0, "end_ms": 100},
                {"word": "未听。", "start_ms": 200, "end_ms": 300},
            ]},
            "expected": {
                "status": "partial", "termination": "interrupted",
                "precision": "provider", "text_chars": 6,
                "heard_chars": 3, "uncertain_chars": 0, "unheard_chars": 3,
            },
        },
        {
            "name": "unknown",
            "text": "边界未知",
            "played_ms": 150,
            "playback_complete": False,
            "termination": "interrupted",
            "expected": {
                "status": "unknown", "termination": "interrupted",
                "precision": "unknown", "text_chars": 4,
                "heard_chars": 0, "uncertain_chars": 4, "unheard_chars": 0,
            },
        },
    ],
    ids=["full", "partial", "unknown"],
)
def test_final_output_settlement_logs_delivery_lengths_and_correlated_ids(case, caplog):
    with caplog.at_level(logging.INFO, logger="voice_terminal.controller.episode_output"):
        captured = asyncio.run(_settle_output_case(case))

    record = captured["record"]
    expected = case["expected"]
    delivery = record["delivery"]
    assert delivery["status"] == expected["status"]
    assert delivery["termination"] == expected["termination"]
    assert delivery["precision"] == expected["precision"]

    speech_id = record["speech_id"]
    episode_id = record["episode_id"]
    request_id = record["request_id"]
    turn_id = record["turn_id"]
    line = next(
        item.getMessage() for item in caplog.records
        if item.name == "voice_terminal.controller.episode_output"
        and "episode.speech_final" in item.getMessage()
        and f"speech_id={speech_id}" in item.getMessage()
    )
    for field, value in (
        ("speech_id", speech_id),
        ("conversation_id", episode_id),
        ("request_id", request_id),
        ("turn_id", turn_id),
        ("delivery", expected["status"]),
        ("text_chars", expected["text_chars"]),
        ("heard_chars", expected["heard_chars"]),
        ("uncertain_chars", expected["uncertain_chars"]),
        ("unheard_chars", expected["unheard_chars"]),
        ("termination", expected["termination"]),
        ("precision", expected["precision"]),
    ):
        assert f"{field}={value}" in line

    # 诊断日志仅含数值和状态（status），播报正文（speech body）不进入日志。
    assert case["text"] not in line
    trace_names = [kind for kind, _payload in captured["traces"]]
    assert "SPEECH_SETTLING_STARTED" in trace_names
    assert trace_names[-1] == "SPEECH_SETTLING_FINISHED"
