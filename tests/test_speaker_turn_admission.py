"""首段认证及整轮文字完整性：分数替身用于状态机，不代表真人识别精度。"""
import asyncio
from dataclasses import replace
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.config import SpeakerConfig
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace
from voice_terminal.core.state import RuntimeState
from voice_terminal.session.ambient import AmbientTranscriptStore
from voice_terminal.speaker.rolling import RollingSpeakerSensor
from voice_terminal.speaker.session import SessionSpeakerPolicy
from voice_terminal.speaker.contracts import SpeakerState


def vector(score):
    return (score, math.sqrt(1-score*score))


def policy(**settings):
    return SessionSpeakerPolicy(SpeakerConfig(**settings), lambda value: value[0])


def observe(p, score, end, *, first=False, quality="ready"):
    return p.observe_turn("a", vector(score) if score is not None else (), start=0, end=end,
                          speech_ms=400, first=first, quality=quality)


def test_first_window_is_not_bypassed_by_calibration_and_recovers_full_prefix():
    p = policy()
    p.activate("manual")
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "开头的几个字必须保留")
    assert p.admitted_text("a") == ""  # 校准宽限不能绕过本轮首段。
    observe(p, .1, 19200, first=True)
    assert turn.phase == "held"
    assert turn.summary()["observation_state"] == SpeakerState.NON_OWNER_CONFIRMED.value
    p.set_turn_text("a", "开头的几个字必须保留，后面也完整")
    # 校准样本不一致不能替代正常主人证据。
    p.calibration_status = "failed"
    for end in (25600, 32000):
        observe(p, .8, end)
        assert p.admitted_text("a") == ""
    observe(p, .8, 38400)
    assert p.admitted_text("a") == "开头的几个字必须保留，后面也完整"
    assert turn.recovered and turn.foreign_voice_suspected and turn.phase == "relaxed"
    assert turn.observation_count == 4 and turn.speech_ms == 1600
    assert len(p.admitted_turns()) == 1
    p.tune(p.config)
    for end in (44800, 51200, 57600):
        observe(p, .8, end)
    assert p.recovered_count == 1  # 已翻转轮次不能因设置清零计数而重复累计恢复。


def test_recovery_requires_three_normal_threshold_results_and_new_audio():
    p = policy(confirm_threshold=.7, continuity_relaxation=.2)
    turn = p.begin_turn("a", start_sample=0)
    observe(p, .1, 19200, first=True)
    for end in (25600, 32000, 38400):
        observe(p, .6, end)
    assert not turn.admitted
    observe(p, .8, 44800)
    for _ in range(5):
        observe(p, .8, 44800)
    assert turn.positive_ticks == 1
    observe(p, .5, 51200)
    assert turn.positive_ticks == 0
    for end in (57600, 64000, 70400):
        observe(p, .8, end)
    assert turn.admitted


def test_owner_admission_survives_negative_scores_and_same_turn_resume():
    p = policy(non_owner_ticks=3)
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "完整句首")
    observe(p, .8, 19200, first=True)
    for end in (25600, 32000, 38400):
        observe(p, .1, end)
    assert turn.admitted and turn.foreign_voice_suspected
    assert p.begin_turn("a", start_sample=50000, generation=1) is turn
    assert turn.start_sample == 0
    p.set_turn_text("a", "完整句首，加上停顿续说", generation=1)
    assert p.admitted_text("a", generation=1) == "完整句首，加上停顿续说"
    assert not p.begin_turn("b", start_sample=100000).admitted


def test_relaxed_threshold_changes_current_observation_without_changing_admission():
    p = policy(confirm_threshold=.7, continuity_relaxation=.2)
    turn = p.begin_turn("a", start_sample=0)
    observe(p, .8, 19200, first=True)
    observe(p, .6, 25600)
    assert turn.state == SpeakerState.OWNER_CONTINUITY and turn.positive_ticks == 1
    assert turn.scores["threshold_used"] == pytest.approx(.5)
    p.tune(replace(p.config, intra_turn_relaxation=0))
    observe(p, .6, 32000)
    assert turn.state == SpeakerState.NON_OWNER_CONFIRMED and turn.positive_ticks == 0
    assert turn.scores["threshold_used"] == pytest.approx(.7)
    assert turn.admitted and turn.owner_confirmed


def test_current_first_sample_cannot_raise_its_own_session_score():
    p = policy(calibration_speech_ms=45)
    p.activate("manual")
    turn = p.begin_turn("a", start_sample=0)
    observe(p, .2, 19200, first=True)
    assert p.session_centroid and turn.scores["effective_session_weight"] == 0
    assert turn.scores["combined_score"] == pytest.approx(.2)
    assert turn.phase == "held" and not turn.admitted


@pytest.mark.parametrize("score,quality", [(None, "unavailable"), (None, "too_quiet")])
def test_uncertain_first_result_preserves_and_admits_text(score, quality):
    p = policy()
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "短句原文")
    observe(p, score, 9600, first=True, quality=quality)
    assert turn.admitted and not turn.owner_confirmed
    assert p.admitted_text("a") == "短句原文"


def test_active_text_does_not_expire_and_closed_turn_cannot_be_released():
    p = policy(pending_ttl_ms=400)
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "保留的开头")
    observe(p, .1, 19200, first=True)
    p.expire(time.monotonic()+1000)
    assert turn.text == "保留的开头"
    p.close_turn("a")
    for end in (25600, 32000, 38400):
        observe(p, .9, end)
    assert not turn.admitted and not p.admitted_turns()


def test_runtime_weight_is_consumed_and_hot_tuning_keeps_admission():
    p = policy()
    p.scorer = lambda _: .2
    p.session_centroid = (1., 0.)
    turn = p.begin_turn("a", start_sample=0)
    p.observe_turn("a", (1., 0.), start=0, end=19200, speech_ms=1200, first=True)
    assert turn.scores["combined_score"] == pytest.approx(.68)
    assert turn.scores["effective_session_weight"] == .6
    p.tune(replace(p.config, session_weight=.2, confirm_threshold=.8, intra_turn_relaxation=.15, continuity_relaxation=.05))
    p.observe_turn("a", (1., 0.), start=0, end=25600, speech_ms=400)
    assert turn.scores["combined_score"] == pytest.approx(.36)
    assert turn.scores["threshold_used"] == pytest.approx(.65)
    assert turn.admitted


def first_controller(score=.8, duration_ms=1200):
    c = object.__new__(EpisodeController)
    c.speaker_config = SpeakerConfig()
    c.speaker_policy = policy()
    c.speaker_policy.activate("system")
    c.speaker_client = SimpleNamespace(embed_pcm=AsyncMock(return_value=vector(score)))
    c.speaker_sensor = RollingSpeakerSensor(c.speaker_client.embed_pcm, lambda v: v[0])
    ws = c.workspace = TurnWorkspace("a", speaker_mode="filter")
    c.speaker_policy.begin_turn("a", start_sample=0)
    pcm = b"\x01\x00" * (duration_ms*16)
    ws.audio.append(AudioFrame(pcm, 0, time.monotonic()))
    c.speaker_sensor.submit_frame(pcm, start_sample=0, speech=True)
    c.state = RuntimeState()
    c.episode = SimpleNamespace(inputs={}, observe=AsyncMock(), episode_id="session")
    c._publish = AsyncMock()
    c._track = lambda task: task
    c._flush_speaker_admissions = AsyncMock()
    return c, ws


@pytest.mark.asyncio
async def test_first_inference_uses_exact_first_1200ms_once_and_accepts_resume():
    c, ws = first_controller(duration_ms=1199)
    assert c.speaker_vetoes_turn("a")  # 校准临时宽限也必须等待本轮首段。
    assert c._ensure_first_speaker_decision(ws) is None
    ws.audio.append(AudioFrame(b"\x01\x00"*16, 1199*16, time.monotonic()))
    task = c._ensure_first_speaker_decision(ws)
    assert c._ensure_first_speaker_decision(ws) is task
    ws.resume()
    c.speaker_policy.begin_turn("a", start_sample=19200, generation=1)
    await task
    assert c.speaker_client.embed_pcm.await_count == 1
    assert len(c.speaker_client.embed_pcm.call_args.args[0]) == 1200*16*2
    assert c.speaker_policy.turns["a"].admitted
    assert not c.speaker_vetoes_turn("a")


@pytest.mark.asyncio
async def test_heartbeat_does_not_start_model_before_current_turn_has_1200ms():
    c, ws = first_controller(duration_ms=1199)
    c.episode.closed = False
    c.episode._heartbeat_episode_generation = 4
    for sequence in (1, 2, 3):
        c._launch_speaker_analysis(4, sequence)
    c.speaker_client.embed_pcm.assert_not_awaited()
    ws.audio.append(AudioFrame(b"\x01\x00"*16, 1199*16, time.monotonic()))
    c._launch_speaker_analysis(4, 4)
    await ws._speaker_first_task
    assert c.speaker_client.embed_pcm.await_count == 1


def test_hot_rolling_context_parameter_reaches_existing_sensor():
    c, _ = first_controller()
    c.speaker_mode = "filter"
    c.owner_only_barge_in = False
    c.speaker_router = SimpleNamespace(score_embedding=lambda v: v[0],
        state_config=SimpleNamespace(confirm_threshold=.6, continuity_threshold=.5))
    sensor = c.speaker_sensor
    c.speaker_config = replace(c.speaker_config, context_max_ms=6400)
    c.speaker_policy.tune(c.speaker_config)
    c.configure_speaker_runtime()
    assert c.speaker_sensor is sensor and sensor.window_samples == 6400*16


def test_calibration_settings_are_frozen_until_next_activation():
    p = policy(calibration_speech_ms=800, calibration_max_ms=2000)
    p.activate("manual", now=0)
    p.begin_turn("a", start_sample=0)
    p.observe_turn("a", (1., 0.), start=0, end=6400, speech_ms=400, first=True, now=.1)
    p.tune(replace(p.config, calibration_speech_ms=1200))
    assert p.calibration_config["calibration_speech_ms"] == 800
    p.observe_turn("a", (1., 0.), start=0, end=12800, speech_ms=400, now=.2)
    assert p.calibration_status == "ready"
    p.clear()
    p.activate("manual", now=.3)
    assert p.calibration_config["calibration_speech_ms"] == 1200


@pytest.mark.asyncio
async def test_short_sentence_and_failed_inference_are_not_empty_input():
    c, ws = first_controller(duration_ms=600)
    c.speaker_client.embed_pcm.side_effect = TimeoutError("请求硬超时")
    await c._ensure_first_speaker_decision(ws, force=True)
    assert len(c.speaker_client.embed_pcm.call_args.args[0]) == 600*16*2
    assert c.speaker_policy.turns["a"].admitted


@pytest.mark.asyncio
async def test_soft_budget_late_result_is_consumed_without_second_inference():
    c, ws = first_controller()
    async def slow(_):
        await asyncio.sleep(.11)
        return vector(.8)
    c.speaker_client.embed_pcm.side_effect = slow
    await c._ensure_first_speaker_decision(ws)
    assert c.speaker_policy.turns["a"].owner_confirmed
    assert c.speaker_policy.timeout_count == 1
    assert c._ensure_first_speaker_decision(ws) is None


@pytest.mark.asyncio
async def test_settings_change_discards_inflight_result_and_recomputes_first():
    c, ws = first_controller()
    ready, release = asyncio.Event(), asyncio.Event()
    async def delayed(_):
        ready.set()
        await release.wait()
        return vector(.8)
    c.speaker_client.embed_pcm.side_effect = delayed
    task = c._ensure_first_speaker_decision(ws)
    await ready.wait()
    c.speaker_policy.tune(replace(c.speaker_config, confirm_threshold=.9))
    release.set()
    await task
    assert c.speaker_policy.turns["a"].phase == "collecting"
    c.speaker_client.embed_pcm.side_effect = None
    await c._ensure_first_speaker_decision(ws)
    assert c.speaker_policy.turns["a"].phase == "held"
    assert c.speaker_policy.last_scores["threshold_used"] == .9


@pytest.mark.parametrize("score,is_owner", [(.59999, False), (.6, True), (.60001, True)])
def test_valid_first_score_is_binary_at_the_effective_threshold(score, is_owner):
    p = policy()
    p.scorer = lambda _: score
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "没有分数灰区，原文仍完整暂存")
    observe(p, .8, 19200, first=True)
    assert turn.admitted is is_owner
    assert turn.scores["identity_result"] == ("owner" if is_owner else "non_owner")
    assert turn.phase == ("relaxed" if is_owner else "held")
    assert turn.text == "没有分数灰区，原文仍完整暂存"


def test_post_admission_below_relaxed_threshold_counts_as_non_owner_without_truncating():
    p = policy(non_owner_ticks=3)
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "句首到句尾保持完整")
    observe(p, .8, 19200, first=True)
    for index, end in enumerate((25600, 32000, 38400), start=1):
        observe(p, .49, end)
        assert turn.scores["identity_result"] == "non_owner"
        assert turn.negative_ticks == index
        assert turn.foreign_voice_suspected is (index == 3)
        assert p.admitted_text("a") == "句首到句尾保持完整"


@pytest.mark.parametrize("weight", [0, .8])
def test_session_weight_slider_extremes_reach_real_combined_score(weight):
    p = policy(session_weight=weight)
    p.scorer = lambda _: .2
    p.session_centroid = (1., 0.)
    turn = p.begin_turn("a", start_sample=0)
    p.observe_turn("a", (1., 0.), start=0, end=19200, speech_ms=1200, first=True)
    assert turn.scores["combined_score"] == pytest.approx(.2 * (1-weight) + weight)
    assert turn.scores["effective_session_weight"] == weight
    assert turn.scores["identity_result"] == ("owner" if weight == .8 else "non_owner")


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_publish_late_first_result():
    c, ws = first_controller()
    task = c._ensure_first_speaker_decision(ws)
    c.speaker_policy.close_turn("a")
    await task
    assert not c.speaker_policy.turns["a"].admitted
    c._flush_speaker_admissions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("score,expected", [(.8, "完整句首和句尾"), (.1, "")])
async def test_commit_routes_full_final_or_ambient_without_early_empty_settlement(score, expected):
    c, ws = first_controller(score)
    ws.final_text = ws.paraformer_final = "完整句首和句尾"
    c.speaker_mode = "filter"
    c.hotwords = None
    c.ambient_store = AmbientTranscriptStore()
    c.episode_history = {}
    c._commit_input_workspace = AsyncMock()
    c._retire_committed_input = AsyncMock()
    c._settle_committed_turn = AsyncMock()
    await c._input_committed(ws, ws.token)
    assert ws.final_text == expected
    assert ws.paraformer_final == "完整句首和句尾"
    assert ws.speaker_decision["speech_ms"] == 1200
    assert ws.speaker_decision["observation_count"] == 1
    assert ws.speaker_decision["speaker_summary"]["admitted"] == bool(expected)
    assert ws.timings["speaker_wait_finished"] >= ws.timings["speaker_inference_completed"]
    if score > .6:
        c._settle_committed_turn.assert_not_awaited()
        assert c.episode.observe.call_args.kwargs["text"] == expected
    else:
        c._settle_committed_turn.assert_awaited_once_with("a", outcome="speaker_blocked")
        assert c.ambient_store.records[0]["text"] == "完整句首和句尾"
        c.episode.observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_result", ["raise", "invalid_vector"])
async def test_first_scoring_failure_admits_full_text_once_and_commits_normally(bad_result):
    c, ws = first_controller()
    if bad_result == "raise":
        def broken(_):
            raise ValueError("评分资料异常")
        c.speaker_policy.scorer = broken
    else:
        c.speaker_client.embed_pcm.return_value = ("无效向量",)
    ws.final_text = ws.paraformer_final = "句首和句尾都完整"
    c.speaker_mode, c.hotwords, c.ambient_store = "filter", None, AmbientTranscriptStore()
    c.episode_history = {}
    c._commit_input_workspace, c._retire_committed_input = AsyncMock(), AsyncMock()
    c._settle_committed_turn = AsyncMock()
    await c._input_committed(ws, ws.token)
    assert c.speaker_client.embed_pcm.await_count == 1
    assert c.speaker_policy.turns["a"].admitted
    assert c.speaker_policy.turns["a"].initial_identity == "unknown"
    assert ws.final_text == "句首和句尾都完整"
    assert c.episode.observe.call_args.kwargs["text"] == ws.final_text
    c._settle_committed_turn.assert_not_awaited()
