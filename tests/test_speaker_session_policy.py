"""会话声纹行为测试：暂存、恢复、迟滞和激活生命周期。"""
from dataclasses import replace

import pytest

from voice_terminal.config import SpeakerConfig
from voice_terminal.speaker.contracts import SpeakerState
from voice_terminal.speaker.session import SessionSpeakerPolicy


def policy(**changes):
    return SessionSpeakerPolicy(replace(SpeakerConfig(), **changes), lambda v: v[0])


def observe(p, vector=(1.0, 0.0), *, turn="a", start=0, end=6400, now=1):
    return p.observe(vector, turn_id=turn, generation=0, start=start, end=end, speech_ms=400, now=now)


def ingest(p, *, turn="a", revision=1, start=0, end=6400, text="你好", now=1):
    p.ingest(turn_id=turn, generation=0, revision=revision, start=start, end=end, text=text, now=now)


def test_unknown_keeps_text_and_later_owner_recovers_same_turn_once():
    p = policy()
    ingest(p)
    assert p.admitted_text("a") == ""
    assert len(p.pieces) == 1
    observe(p)
    assert p.admitted_text("a") == "你好"
    ingest(p)
    assert p.admitted_text("a") == "你好"
    assert p.recovered_count == 1


def test_pending_other_voice_is_not_released_by_later_owner():
    p = policy()
    observe(p, (0.4, 0.916515), end=6400)
    ingest(p, text="别人的开头")
    observe(p, start=6400, end=12800, now=2)
    assert p.admitted_text("a") == ""
    assert p.pieces[0].status == "pending"


def test_cross_turn_match_recovery_preserves_order_and_rejects_other_voice():
    score = [0.4]
    p = SessionSpeakerPolicy(SpeakerConfig(), lambda _: score[0])
    observe(p)
    ingest(p, text="前一句")
    score[0] = 0.9
    observe(p, turn="b", start=6400, end=12800, now=2)
    ingest(p, turn="b", start=6400, end=12800, text="后一句", now=2)
    assert [row["text"] for row in p.admitted_turns()] == ["前一句", "后一句"]


def test_owner_jitter_and_timeouts_do_not_revoke_admitted_text():
    p = policy()
    observe(p)
    ingest(p)
    assert observe(p, (0.4, 0.916515), start=6400, end=12800, now=2) == SpeakerState.OWNER_CONFIRMED
    # 同一末端重复推理不能累计持续反证。
    for _ in range(8):
        observe(p, (0.0, 1.0), end=12800, now=3)
    assert p.state("a", 0) == SpeakerState.OWNER_CONFIRMED
    assert p.admitted_text("a") == "你好"


def test_three_distinct_negative_ticks_block_only_future_text():
    p = policy()
    observe(p)
    ingest(p)
    for index in range(3):
        state = observe(p, (0.0, 1.0), start=6400*(index+1), end=6400*(index+2), now=index+2)
    assert state == SpeakerState.NON_OWNER_CONFIRMED
    ingest(p, revision=2, start=25600, end=32000, text="外人", now=5)
    assert p.admitted_text("a") == "你好"
    assert p.pieces[-1].status == "rejected"


def test_pending_expiry_and_turn_limit():
    p = policy(pending_ttl_ms=400, pending_max_turns=1)
    ingest(p, turn="a", now=1)
    ingest(p, turn="b", now=1.1)
    assert [x.turn_id for x in p.pieces] == ["b"]
    p.expire(2)
    assert p.pieces == []


@pytest.mark.parametrize("source", ["manual", "kws"])
def test_calibration_grants_initial_input_freezes_across_turns_and_clears(source):
    p = policy(calibration_speech_ms=800, calibration_max_ms=1000)
    p.activate(source, now=1)
    activation = p.activation_id
    ingest(p, now=1)
    # 注入测试时钟，不通过真实等待验证期限。
    assert p.grace(1)
    observe(p, end=6400, now=1.1)
    observe(p, start=6400, end=12800, now=1.5)
    assert p.calibration_status == "ready"
    centroid = p.session_centroid
    p.activate(source, now=2)
    assert p.activation_id == activation
    observe(p, turn="b", vector=(0, 1), start=12800, end=19200, now=2)
    assert p.session_centroid == centroid
    p.clear()
    assert not p.session_centroid and not p.pieces and not p.activation_id


def test_calibration_timeout_does_not_extend_on_duplicate_activation():
    p = policy(calibration_speech_ms=800, calibration_max_ms=1000)
    p.activate("manual", now=1)
    p.activate("kws", now=1.8)
    assert not p.grace(2.1)
    assert p.calibration_status == "failed"
    assert not p.session_centroid


def test_invalid_counter_and_nonfinite_values_rejected():
    with pytest.raises(ValueError):
        SpeakerConfig(non_owner_ticks=0)
    with pytest.raises(ValueError):
        SpeakerConfig(session_weight=float("nan"))


def test_calibration_failure_removes_temporary_identity_without_revoking_text():
    p=policy(calibration_speech_ms=800,calibration_max_ms=1000)
    p.activate("manual",now=1)
    observe(p,(0,1),now=1.1)
    ingest(p,now=1.2)
    assert p.state("a",0,now=2.1)==SpeakerState.UNKNOWN
    assert p.admitted_text("a")=="你好"


def test_inconsistent_calibration_sample_keeps_existing_candidate():
    p=policy(calibration_speech_ms=800)
    p.activate("kws",now=1)
    observe(p,now=1.1)
    observe(p,(0,1),start=6400,end=12800,now=1.5)
    assert p.calibration_speech_ms==400
    observe(p,start=12800,end=19200,now=1.9)
    assert p.calibration_status=="ready"
    assert p.session_centroid==(1.0,0.0)


def test_confirmed_nonowner_prefix_is_never_restored_later():
    score=[.1]
    p=SessionSpeakerPolicy(SpeakerConfig(),lambda _:score[0])
    observe(p)
    ingest(p,text="异人正文")
    observe(p,start=6400,end=12800,now=2)
    observe(p,start=12800,end=19200,now=3)
    assert p.pieces[0].status=="rejected"
    score[0]=.9
    observe(p,turn="b",start=19200,end=25600,now=4)
    assert p.admitted_text("a")==""


def test_hot_settings_apply_without_rebuilding_fixed_centroid():
    p=policy(calibration_speech_ms=400)
    p.activate("manual",now=1)
    observe(p,now=1.1)
    centre=p.session_centroid
    p.tune(replace(p.config,session_weight=.2,confirm_threshold=.8,continuity_relaxation=.1,
                   calibration_speech_ms=800))
    observe(p,(.7,.7141428),turn="b",start=6400,end=12800,now=2)
    assert p.session_centroid==centre
    assert p.last_scores["combined_score"]==pytest.approx(.7,abs=.001)
    assert p.settings_revision==1
    assert p.calibration_config["calibration_speech_ms"]==400
