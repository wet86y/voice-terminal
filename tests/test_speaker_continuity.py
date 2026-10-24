"""统一语音轮次边界、独立放宽量及开口资格快照的行为回归。"""
from dataclasses import replace
import asyncio
from collections import deque
import math
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from voice_terminal.config import SpeakerConfig
from voice_terminal.speaker.contracts import SpeakerState
from voice_terminal.speaker.session import SessionSpeakerPolicy


def make_policy(**values):
    config = SpeakerConfig(**{"confirm_threshold": .7, "continuity_relaxation": .1,
                              "intra_turn_relaxation": .2, **values})
    return SessionSpeakerPolicy(config, lambda vector: vector[0])


def sample(policy, turn_id, score, end=19200, *, first=True, now=20, quality="ready"):
    vector = () if score is None else (score, math.sqrt(1-score*score))
    return policy.observe_turn(turn_id, vector, start=0, end=end, speech_ms=400,
                               first=first, now=now, quality=quality)


def finish(policy, turn_id, end_at, *, reason="smart_turn"):
    policy.boundary(turn_id, at=end_at, reason=reason)
    policy.close_turn(turn_id, completed=True)


def previous_owner(policy, *, end_at=2):
    policy.begin_turn("a", start_sample=0, started_at=0)
    sample(policy, "a", .8)
    finish(policy, "a", end_at)


def test_baseline_intra_and_cross_amounts_have_distinct_consumers_without_stacking():
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .65)
    assert turn.scores["threshold_used"] == pytest.approx(.6)
    assert turn.scores["threshold_scope"] == "cross_turn"
    assert turn.state == SpeakerState.OWNER_CONTINUITY and turn.normal_owner_epoch is None
    p.set_turn_text("b", "完整开头和后半句")
    sample(p, "b", .55, end=25600, first=False)
    assert turn.scores["threshold_used"] == pytest.approx(.5)
    assert turn.scores["threshold_scope"] == "intra_turn"
    assert turn.state == SpeakerState.OWNER_CONTINUITY
    assert p.admitted_text("b") == "完整开头和后半句"


def test_runtime_audit_links_real_scope_values_start_snapshot_and_final_source(caplog):
    caplog.set_level(logging.INFO, logger="voice_terminal.llm_context_audit")
    p = make_policy()
    previous_owner(p, end_at=2)
    p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .65)
    sample(p, "b", .55, end=25600, first=False)
    finish(p, "b", 4)
    records = [json.loads(row.message) for row in caplog.records if row.name == "voice_terminal.llm_context_audit"]
    scores = [row for row in records if row.get("turn_id") == "b" and row["kind"].startswith("inference_result")]
    assert [row["threshold_scope"] for row in scores] == ["cross_turn", "intra_turn"]
    assert [row["threshold_used"] for row in scores] == pytest.approx([.6, .5])
    assert all(row["intra_turn_relaxation"] == .2 and row["continuity_relaxation"] == pytest.approx(.1) for row in scores)
    assert scores[0]["field_labels"]["threshold_scope"] == "实际阈值适用范围"
    assert scores[0]["value_labels"]["cross_turn"] == "跨轮放宽"
    start = next(row for row in records if row.get("turn_id") == "b" and row["kind"].startswith("turn_started"))
    assert start["continuity_grant"]["source_turn_id"] == "a"
    assert start["captured_at"] == 3
    final = [row for row in records if row["kind"].startswith("continuity_boundary")][-1]
    assert final["continuity"]["normal_end_at"] == 2 and final["boundary_at"] == 4


def test_continuity_uses_audio_endpoint_not_inference_or_asr_completion_time():
    p = make_policy()
    previous_owner(p, end_at=2)  # 推理完成时刻是20，不能把有效期推迟到25。
    assert p.continuity.normal_end_at == 2
    turn = p.begin_turn("b", start_sample=32000, started_at=7.01)
    sample(p, "b", .65, now=25)
    assert not turn.continuity_grant
    assert turn.scores["threshold_scope"] == "normal"
    assert not turn.owner_confirmed and not turn.admitted  # 资格到期，低于正常阈值即暂缓。


def test_start_snapshot_survives_expiry_during_first_audio_and_inference():
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=6.99)
    sample(p, "b", .65, now=20)
    assert turn.admitted and turn.state == SpeakerState.OWNER_CONTINUITY
    finish(p, "b", 20)
    late = p.begin_turn("c", start_sample=64000, started_at=20.1)
    assert not late.continuity_grant  # 已过最长时限，不因慢推理或闭段续期。


def test_only_relaxed_results_extend_chain_to_original_max_but_never_refresh_it():
    p = make_policy()
    previous_owner(p, end_at=2)
    p.begin_turn("b", start_sample=32000, started_at=6)
    sample(p, "b", .65)
    finish(p, "b", 8)
    assert p.continuity.normal_end_at == 2
    assert p.continuity.source_turn_id == "b" and p.continuity.normal_turn_id == "a"
    turn = p.begin_turn("c", start_sample=64000, started_at=11.9)
    assert turn.continuity_grant["continuity_deadline"] == 12
    sample(p, "c", .65)
    finish(p, "c", 13)
    assert not p.begin_turn("d", start_sample=96000, started_at=13.1).continuity_grant


def test_new_normal_owner_result_renews_only_at_its_final_boundary():
    p = make_policy()
    previous_owner(p)
    p.begin_turn("b", start_sample=32000, started_at=6)
    sample(p, "b", .8, now=100)
    assert p.continuity.normal_end_at == 2
    finish(p, "b", 8)
    assert p.continuity.normal_end_at == 8 and p.continuity.normal_turn_id == "b"
    assert p.begin_turn("c", start_sample=64000, started_at=12).continuity_grant


@pytest.mark.parametrize("score,quality", [(None, "unavailable"), (None, "too_quiet")])
def test_unknown_or_timeout_preserves_original_qualification_without_renewing(score, quality):
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=4)
    sample(p, "b", score, quality=quality)
    assert turn.first_pending and not turn.admitted and not turn.owner_confirmed
    finish(p, "b", 6)
    assert p.continuity.normal_end_at == 2 and not p.continuity.continuing
    assert not p.begin_turn("c", start_sample=64000, started_at=7.1).continuity_grant


def test_sustained_non_owner_revokes_future_qualification_without_truncating_current_text():
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .8)
    p.set_turn_text("b", "句首和异声插入后的文字都保留")
    for end in (25600, 32000, 38400):
        sample(p, "b", .1, end=end, first=False)
    assert turn.admitted and turn.foreign_voice_suspected
    assert p.admitted_text("b") == "句首和异声插入后的文字都保留"
    finish(p, "b", 4)
    assert not p.begin_turn("c", start_sample=64000, started_at=5).continuity_grant


def test_first_negative_keeps_existing_counter_semantics_and_recovery_uses_baseline():
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .1)
    assert turn.negative_ticks == 0 and p.continuity.normal_end_at == 2
    for end in (25600, 32000, 38400):
        sample(p, "b", .65, end=end, first=False)
    assert not turn.admitted and turn.positive_ticks == 0
    for end in (44800, 51200, 57600):
        sample(p, "b", .8, end=end, first=False)
    assert turn.admitted and turn.recovered


def test_held_turn_with_only_one_normal_result_cannot_renew_next_turn_qualification():
    p = make_policy()
    previous_owner(p, end_at=2)
    turn = p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .1)
    sample(p, "b", .8, end=25600, first=False)
    assert not turn.admitted and turn.positive_ticks == 1
    finish(p, "b", 8)
    assert p.continuity.normal_turn_id == "a" and p.continuity.normal_end_at == 2
    assert not p.begin_turn("c", start_sample=64000, started_at=8.1).continuity_grant


def test_candidate_boundary_can_be_revoked_by_resume_and_only_final_end_renews():
    p = make_policy()
    turn = p.begin_turn("a", start_sample=100, started_at=0)
    sample(p, "a", .8)
    p.boundary("a", at=2, reason="smart_turn")
    assert p.continuity.normal_end_at is None
    p.resume_turn("a")
    assert turn.boundary_at is None
    assert p.begin_turn("a", start_sample=20000, generation=1) is turn
    assert turn.start_sample == 100 and turn.admitted
    finish(p, "a", 4, reason="hard_silence")
    assert p.continuity.normal_end_at == 4 and turn.boundary_revision == 3
    p.close_turn("a", completed=True)
    assert p.continuity.normal_end_at == 4


@pytest.mark.parametrize("setting,value", [("intra_turn_relaxation", .1), ("continuity_relaxation", .05),
    ("confirm_threshold", .8), ("session_weight", .3), ("continuity_window_ms", 1000)])
def test_hot_scoring_settings_clear_old_qualification_but_keep_admitted_text(setting, value):
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .8)
    p.set_turn_text("b", "不撤销已获准文本")
    p.tune(replace(p.config, **{setting: value}))
    assert turn.admitted and p.admitted_text("b") == "不撤销已获准文本"
    finish(p, "b", 4)
    assert not p.begin_turn("c", start_sample=64000, started_at=5).continuity_grant


def test_closed_record_eviction_and_enrollment_only_change_do_not_revoke_qualification():
    p = make_policy(pending_max_turns=1)
    previous_owner(p)
    p.tune(replace(p.config, enrollment_min_total_speech_ms=18000))
    p.begin_turn("b", start_sample=32000, started_at=3)
    sample(p, "b", .4)
    finish(p, "b", 4)
    assert "a" not in p.turns
    assert p.begin_turn("c", start_sample=64000, started_at=5).continuity_grant["source_turn_id"] == "a"


def test_barge_candidate_reads_same_start_snapshot_without_renewing_closed_turns():
    p = make_policy()
    previous_owner(p)
    p.begin_candidate("candidate", start_sample=32000, started_at=6.9)
    sample(p, "barge-candidate", .65, first=False, now=20)
    assert p.state("barge-candidate", 0) == SpeakerState.OWNER_CONTINUITY
    assert p.continuity.normal_end_at == 2 and not p.continuity.continuing
    p.begin_candidate("expired", start_sample=64000, started_at=7.1)
    sample(p, "barge-expired", .65, first=False)
    assert p.state("barge-expired", 0) == SpeakerState.NON_OWNER_CONFIRMED
    assert p.candidates["barge-expired"].scores["identity_result"] == "non_owner"


@pytest.mark.parametrize("filtering", [True, False])
def test_abnormal_close_and_activation_reset_cannot_publish_qualification(filtering):
    p = make_policy()
    previous_owner(p)
    turn = p.begin_turn("b", start_sample=32000, started_at=3, filtering=filtering)
    sample(p, "b", .8, first=filtering)
    p.boundary("b", at=4, reason="smart_turn")
    p.close_turn("b", reason="failed")
    assert not p.begin_turn("c", start_sample=64000, started_at=5).continuity_grant
    sample(p, "b", .9, end=64000, first=False)
    assert turn.closed and p.continuity.normal_end_at is None
    p.clear()
    assert p.continuity.normal_end_at is None and not p.candidates


@pytest.mark.parametrize("reason", ["smart_turn", "hard_silence", "max_duration", "vad_end"])
@pytest.mark.parametrize("ongoing", [False, True])
def test_real_freeze_records_audio_boundary_without_publishing_qualification_before_commit(reason, ongoing):
    from voice_terminal.controller.realtime_controller import RealtimeController
    from voice_terminal.controller.turn_workspace import TurnWorkspace, AudioFrame
    async def scenario():
        p = make_policy()
        p.begin_turn("a", start_sample=0, started_at=0)
        sample(p, "a", .8)
        c = object.__new__(RealtimeController)
        c.speaker_policy, c.speaker_config = p, p.config
        ws = c.workspace = TurnWorkspace("a", speaker_mode="filter")
        ws.timings["last_speech"] = 2
        ws.hard_closed = reason == "max_duration"
        c._speech_start_sample, c._active_input = (16000 if ongoing else None), True
        c._schedule_segment_refinements, c._queue_asr, c._track = Mock(), Mock(), Mock()
        c._detail, c._state_event, c._complete = AsyncMock(), AsyncMock(), AsyncMock()
        c._sub_id, c._refine_task = "asr-a", None
        await c._freeze(reason, AudioFrame(b"\x00\x00"*320, 19200, 10))
        await c._completion_task
        expected_end = 10 if ongoing and reason == "max_duration" else 2
        assert p.turns["a"].boundary_at == expected_end and p.turns["a"].boundary_reason == reason
        assert p.continuity.normal_end_at is None
        p.close_turn("a", completed=True)
        assert p.continuity.normal_end_at == expected_end
    asyncio.run(scenario())


def test_hot_update_drops_candidate_old_owner_proof_as_well_as_inherited_qualification():
    p = make_policy()
    previous_owner(p)
    p.begin_candidate("candidate", start_sample=32000, started_at=3)
    sample(p, "barge-candidate", .8, first=False)
    p.tune(replace(p.config, session_weight=.3))
    assert p.state("barge-candidate", 0) == SpeakerState.UNKNOWN
    sample(p, "barge-candidate", .65, end=25600, first=False)
    assert p.state("barge-candidate", 0) == SpeakerState.NON_OWNER_CONFIRMED


def test_frozen_wait_crossing_max_duration_is_hard_boundary_before_new_input_cancel():
    from voice_terminal.controller.realtime_controller import RealtimeController
    from voice_terminal.controller.options import InputOptions
    from voice_terminal.controller.turn_workspace import TurnWorkspace, AudioFrame
    async def scenario():
        p = make_policy()
        p.begin_turn("a", start_sample=0, started_at=0)
        sample(p, "a", .8)
        p.boundary("a", at=.5, reason="smart_turn")
        c = object.__new__(RealtimeController)
        ws = c.workspace = TurnWorkspace("a", started_at=0, speaker_mode="filter")
        ws.freeze()
        assert not ws.hard_closed
        c.state = SimpleNamespace(listening=True)
        c.options = InputOptions(max_turn_ms=1000)
        c.audio_processor, c.player = None, None
        c._active_input, c.wake_enabled, c.wake_always_on = False, False, False
        c._buffer_wake_frame = AsyncMock(return_value=False)
        c._asr_queue_overflow, c._reset_vad_pending = False, False
        c._last_frame, c._vad_origin, c._speech_end_sample = None, 0, None
        c._recent, c.wake_preroll_ms, c._vad_generation = deque(), 0, 0
        c._endpoint_task, c._resume_started, c._speech_samples = None, None, 0
        c.vad_min_speech_ms = 45
        activity = SimpleNamespace(speech_started=True, speech_ended=False,
            speech_start_sample=0, speech_end_sample=None, is_speech=True)
        c.vad = SimpleNamespace(feed=lambda _pcm: activity)
        c._cancel_tts_hold_fallback = Mock()
        c._observe_activity = AsyncMock(return_value=activity)
        c._perception_frame, c._near_voice_activity = Mock(), Mock(return_value=True)
        c._barge_in_protected_available = Mock(return_value=True)
        c.barge_in_gate = SimpleNamespace(feed=Mock(return_value=SimpleNamespace(
            confirmed=False, candidate=True, duration_ms=200, reason="candidate")), prime=Mock(),
            candidate_started_at=3)
        c.turns = SimpleNamespace(invalidate=Mock())
        c._stop_playback_snapshot = Mock(return_value={})
        c._state_event, c._record_interruption, c._new_turn = AsyncMock(), AsyncMock(), AsyncMock()
        async def cancel(**_kwargs):
            assert ws.hard_closed
            p.close_turn("a", completed=ws.hard_closed)
        c._cancel_turn = AsyncMock(side_effect=cancel)
        await c.handle_frame(AudioFrame(b"\x00\x00"*3200, 0, 3))
        c._new_turn.assert_awaited_once()
        assert p.continuity.normal_end_at == .5
    asyncio.run(scenario())
