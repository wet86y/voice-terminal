"""取消替换的真实采样、代次隔离及传达边界验证，不打开音频设备。"""
from types import SimpleNamespace
import asyncio

import numpy as np
import pytest

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.agent.options import AgentOptions
from voice_terminal.tts.alignment import DeliveryResolver
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.agent.decision_application import apply_decision_v06
from voice_terminal.agent.decision_envelope import DecisionEnvelope, SpeechDecision


def harness(monkeypatch, rate=1000, channels=1, remaining_ms=1000):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("voice_terminal.audio.player.time.monotonic", lambda: clock.now)
    rendered, started = [], []
    player = PcmPlayer(sample_rate=rate, channels=channels, output_device=None,
                       on_audio_rendered=rendered.append, on_playback_started=started.append)
    player.allow("old")
    pcm = np.full((round((remaining_ms + 20) * rate / 1000), channels), 10000, dtype="<i2")
    player.enqueue("old", pcm.tobytes())
    player._callback(bytearray(round(.02 * rate) * channels * 2), round(.02 * rate), None, None)
    transition, _ = player.prepare_replacement("old")
    return player, transition, clock, rendered, started


def render(player, clock, milliseconds=20):
    frames = round(milliseconds * player.sample_rate / 1000)
    out = bytearray(frames * player.channels * 2)
    player._callback(out, frames, None, None)
    clock.now += milliseconds / 1000
    return np.frombuffer(out, dtype="<i2").reshape(-1, player.channels)


@pytest.mark.parametrize("rate,channels", [(16000, 1), (24000, 2), (32000, 1), (48000, 2)])
def test_two_stage_envelope_preserves_source_order_and_real_reference(monkeypatch, rate, channels):
    player, transition, clock, references, started = harness(monkeypatch, rate, channels)
    output = np.concatenate([render(player, clock) for _ in range(26)])
    assert np.allclose(output[round(.3 * rate)], 3000, atol=3)
    assert np.max(output[:round(.5 * rate)]) <= 10000
    assert np.all(np.diff(output[:round(.5 * rate), 0].astype(int)) <= 1)
    assert not output[round(.5 * rate):].any()
    assert transition.done
    assert started == ["old"]
    assert b"".join(references[1:]) == output.astype("<i2").tobytes()
    assert transition.snapshot(device_latency_ms=0).played_ms == pytest.approx(520, abs=1)


def test_early_audio_uses_short_old_exit_then_new_fade_in(monkeypatch):
    player, transition, clock, _, started = harness(monkeypatch)
    for _ in range(5):
        render(player, clock)
    assert player.bind_replacement(transition, "new")
    player.allow("new")
    player.enqueue("new", np.full(200, -10000, dtype="<i2").tobytes())
    # 旧合成迟到回传即使仍在运行也不能进入播放器。
    player.enqueue("old", np.full(100, 30000, dtype="<i2").tobytes())
    output = np.concatenate([render(player, clock) for _ in range(4)])[:, 0]
    assert (output[:30] >= 0).all()
    assert output[30] == 0
    assert (output[31:] < 0).all()
    assert output[60] == -10000
    assert started == ["old", "new"]
    assert transition.reason == "early_handoff"
    assert player.progress("new")["played_ms"] == 50
    assert transition.snapshot(device_latency_ms=0).played_ms == 150


def test_unready_successor_does_not_cut_old_early(monkeypatch):
    player, transition, clock, _, started = harness(monkeypatch)
    player.bind_replacement(transition, "new")
    player.allow("new")
    player.configure_stream_buffer("new", startup_ms=200, resume_ms=0)
    player.enqueue("new", np.full(100, -10000, dtype="<i2").tobytes())
    for _ in range(25):
        render(player, clock)
    assert "new_audio_ready" not in transition.events
    assert started == ["old"]
    player.finish_stream("new")
    assert (render(player, clock)[1:] < 0).all()


def test_repeated_replacement_keeps_original_gain_and_deadline(monkeypatch):
    player, transition, clock, _, _ = harness(monkeypatch)
    render(player, clock, 100)
    player.bind_replacement(transition, "new")
    player.allow("new")
    player.enqueue("new", b"\x01\x00" * 100)
    again, stopped = player.prepare_replacement("new")
    assert again is transition
    assert not stopped.started
    assert transition.position == 100
    assert transition.started_at == 10
    assert player.bind_replacement(transition, "latest")
    player.allow("latest")
    assert render(player, clock)[0, 0] == pytest.approx(7667, abs=2)


@pytest.mark.parametrize("stop", ["clear", "stop", "permission"])
def test_stop_and_permission_revoke_clear_transition_immediately(monkeypatch, stop):
    player, transition, clock, _, _ = harness(monkeypatch)
    if stop == "permission":
        player.playback_permission = lambda: False
    elif stop == "stop":
        player.stop_and_snapshot()
    else:
        player.clear()
    assert not render(player, clock).any()
    assert transition.done


def test_deadline_handles_stalled_callbacks_without_replaying_old_audio(monkeypatch):
    player, transition, clock, _, _ = harness(monkeypatch)
    clock.now += .7
    assert not render(player, clock).any()
    assert transition.reason == "deadline"
    assert transition.position == 0


def test_short_source_ends_without_repetition_and_unstarted_source_is_silent(monkeypatch):
    player, transition, clock, _, _ = harness(monkeypatch, remaining_ms=30)
    output = np.concatenate([render(player, clock) for _ in range(3)])[:, 0]
    assert not output[30:].any()
    assert transition.position == 30
    player.allow("unstarted")
    player.enqueue("unstarted", b"\x01\x00" * 100)
    unstarted, _ = player.prepare_replacement("unstarted")
    assert unstarted.done and unstarted.position == 0
    assert not render(player, clock).any()


def test_lower_legacy_cap_scales_stages_and_large_legacy_cap_is_clamped(monkeypatch):
    player, _, clock, _, _ = harness(monkeypatch)
    player.allow("old")
    player.enqueue("old", np.full(1020, 10000, dtype="<i2").tobytes())
    render(player, clock)
    transition, _ = player.prepare_replacement("old", max_ms=200)
    output = np.concatenate([render(player, clock) for _ in range(11)])[:, 0]
    assert output[120] == pytest.approx(3000, abs=3)
    assert not output[200:].any()
    assert AgentOptions.from_mapping({"replace_grace_ms": 1000}).replace_grace_ms == 1000
    assert AgentOptions().replace_grace_ms == 600
    assert transition.max_ms == 200


def test_attenuated_words_remain_uncertain_even_when_pcm_was_submitted():
    resolver = DeliveryResolver()
    resolver.add_text("已正常传达。", 0)
    resolver.mark_boundary(100)
    resolver.add_text("被淡出的话。", 100)
    resolver.mark_boundary(400)
    resolver.add_text("没有播放。", 400)
    resolver.mark_boundary(800)
    delivery = resolver.resolve({"started": True, "output_estimated_ms": 400},
                                "已正常传达。被淡出的话。没有播放。", attenuated_range_ms=(100, 400))
    assert delivery["heard_text"] == "已正常传达。"
    assert delivery["uncertain_text"] == "被淡出的话。"
    assert delivery["unheard_text"] == "没有播放。"


def test_delayed_old_settlement_cannot_replace_new_delivery_frontier():
    output = EpisodeSpeechOutput(SimpleNamespace(tts=None, player=None, options=None, bridge_options=None))
    def record(generation, text, turn="turn"):
        return {"speech_id": str(generation), "output_generation": generation, "turn_id": turn,
                "delivery": {"heard_text": text, "status": "partial", "termination": "replaced"}}
    output.record_delivery(record(2, "新回答。"))
    output.record_delivery(record(1, "旧前缀。"))
    assert output.delivered["text"] == "旧前缀。新回答。"
    assert output.last_delivery["heard_text"] == "新回答。"
    output.record_delivery(record(0, "上一轮。", "previous"))
    assert output.delivered["turn_id"] == "turn"


def test_stale_cancel_reassesses_without_replacing_another_generation():
    async def scenario():
        seen = []
        async def replace(*args, **kwargs):
            assert kwargs["expected_generation"] == 2
            return None
        async def log(*args, **kwargs):
            seen.append(args[0])
        runtime = SimpleNamespace(
            output=SimpleNamespace(speak_snapshot={"output_generation": 3}, replace_current=replace),
            _applying_speech_admission=SimpleNamespace(snapshot={"output_generation": 2}),
            minor_protocol_feedback=[], log=log, _wake=lambda kind, **_: seen.append(kind))
        await apply_decision_v06(runtime, ("conversation", "request"), "turn",
                                 DecisionEnvelope(SpeechDecision("cancel", "calm", "最新回答。"), ()))
        assert runtime.current_action == "wait"
        assert seen == ["SPEECH_CANCEL_REASSESS", "SPEECH_CANCEL_REJECTED"]
    asyncio.run(scenario())


def test_replacement_revokes_old_audio_before_async_diagnostics():
    async def scenario():
        from tests.test_speech_continuation import ContinuationHarness
        harness = ContinuationHarness()
        entered, release = asyncio.Event(), asyncio.Event()
        task = None
        try:
            old = await harness.first()
            harness.render(20)
            async def progress(*args, **kwargs):
                if len(args) > 1 and args[1] == "replacement_detected":
                    entered.set()
                    await release.wait()
            harness.output._emit_handle_progress = progress
            task = asyncio.create_task(harness.output.replace_current(harness.identity, harness.turn_id))
            await asyncio.wait_for(entered.wait(), .5)
            assert old.reason == "replaced"
            assert not harness.output._valid(old.workspace.token)
            assert harness.player._replacement is not None
            assert harness.output.busy  # 已撤销句柄不活跃时，旧音收尾仍阻止空闲切换。
            before = harness.player.buffered_ms
            harness.player.enqueue(old.workspace.playback_key, b"\xff\x7f" * 100)
            assert harness.player.buffered_ms == before
        finally:
            release.set()
            if task is not None:
                await task
            await harness.close()
    asyncio.run(scenario())


def test_active_delivery_uses_own_alignment_and_stale_target_is_not_cancelled():
    async def scenario():
        from tests.test_speech_continuation import ContinuationHarness, FIRST_TEXT
        harness = ContinuationHarness()
        try:
            old = await harness.first()
            harness.output.last_delivery = {"heard_text": "其他事务的已听文本。"}
            harness.output._delivery_resolvers[old.speech_id].add_provider_alignment([
                {"word": FIRST_TEXT[:2], "start_ms": 0, "end_ms": 10},
                {"word": FIRST_TEXT[2:], "start_ms": 70, "end_ms": 80},
            ])
            harness.render(20)
            snapshot = harness.output.speak_snapshot
            assert snapshot["delivery"]["heard_text"] == FIRST_TEXT[:2]
            assert snapshot["delivery"]["speech_id"] == old.speech_id
            rejected = await harness.output.replace_current(harness.identity, harness.turn_id,
                expected_generation=harness.output.generation - 1)
            assert rejected is None
            assert old.reason is None
            assert harness.output._valid(old.workspace.token)
            assert harness.player.buffered_ms == 60
        finally:
            await harness.close()
    asyncio.run(scenario())
