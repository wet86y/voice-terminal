"""声纹增长上下文与采集范围：不等待旧窗口，不重复发送音频。"""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace
from voice_terminal.speaker.rolling import RollingSpeakerSensor, RollingSpeakerIdentity


def make_frame(start,samples=320):
    return AudioFrame(b"\xe8\x03"*samples,start,1.0)


def test_45ms_input_at_sample_zero_can_be_analyzed():
    async def run():
        embed=AsyncMock(return_value=(1.,0.))
        sensor=RollingSpeakerSensor(embed,lambda _:0.8)
        sensor.submit_frame(b"\xe8\x03"*720,start_sample=0,speech=True)
        result=await sensor.analyze(candidate_start_sample=0,candidate_end_sample=720)
        assert result.identity==RollingSpeakerIdentity.OWNER
        assert (result.analysis_start_sample,result.analysis_end_sample)==(0,720)
        assert result.speech_ms==45
        assert (await sensor.analyze()).reason=="no_new_audio"
        assert embed.await_count==1
    asyncio.run(run())


def test_candidate_range_excludes_previous_speaker_audio():
    async def run():
        embed=AsyncMock(return_value=(1.,0.))
        sensor=RollingSpeakerSensor(embed,lambda _:0.8)
        sensor.submit_frame(b"\x00\x10"*3200,start_sample=0,speech=True)
        current=b"\xe8\x03"*1600
        sensor.submit_frame(current,start_sample=3200,speech=True)
        result=await sensor.analyze(candidate_start_sample=3200,candidate_end_sample=4800)
        assert result.analysis_start_sample==3200
        assert embed.await_args.args[0]==current
    asyncio.run(run())


def test_duplicate_frames_do_not_add_evidence_and_gap_resets_context():
    sensor=RollingSpeakerSensor(AsyncMock(),lambda _:0.8)
    pcm=b"\xe8\x03"*720
    assert sensor.submit_frame(pcm,start_sample=0,speech=True)
    assert not sensor.submit_frame(pcm,start_sample=0,speech=True)
    assert sensor.buffered_samples==720
    assert sensor.submit_frame(pcm,start_sample=1600,speech=True)
    assert sensor.buffered_samples==720


def test_turn_priming_does_not_start_duplicate_worker_stream():
    c=object.__new__(RealtimeController)
    c.speaker_client=SimpleNamespace(submit_frame=Mock())
    c._prime_speaker_turn_audio(TurnWorkspace("a"),make_frame(0))
    c.speaker_client.submit_frame.assert_not_called()


def test_resume_increments_generation_before_router_reset_and_preroll():
    """真实调用 _resume()，验证 generation（代次）和前滚帧采用新代次。"""
    ws = TurnWorkspace("resume", generation=3, speaker_mode="observe")
    ws.timings.update(turn_started=1.0, first_segment=2.0, asr_first_text=3.0)
    submit = Mock(return_value=True)
    router_reset = Mock()
    current = make_frame(640)

    controller = object.__new__(RealtimeController)
    controller.workspace = ws
    controller._speech_start_sample = 320
    controller._resume_started = 320
    controller.speaker_mode = "filter"
    controller.speaker_router = SimpleNamespace(reset=router_reset)
    controller.speaker_client = SimpleNamespace(submit_frame=submit, degraded=False)
    controller._speaker_turn_start_sample = None
    controller._recent = deque((make_frame(320), current))
    # playback_hold_active（播放保持标记）绕过无关的播放输出快照路径。
    controller.output = SimpleNamespace(playback_hold_active=True)
    controller._stop_generation = Mock(return_value=[])
    controller._reset_tool_scope = Mock()
    controller._new_sub = Mock()
    controller._detail = AsyncMock()
    controller._state_event = AsyncMock()
    controller._turn_timings = {}
    controller.analysis_health = {}
    controller.state = SimpleNamespace(current={})
    controller.barge_in_gate = SimpleNamespace(reset=Mock())
    from voice_terminal.config import SpeakerConfig
    from voice_terminal.speaker.session import SessionSpeakerPolicy
    controller.speaker_config = SpeakerConfig()
    controller.speaker_policy = SessionSpeakerPolicy(controller.speaker_config, lambda _vector: .8)
    turn = controller.speaker_policy.begin_turn("resume", start_sample=320, generation=3, started_at=1)
    controller.speaker_policy.observe_turn("resume", (1., 0.), start=320, end=19520, speech_ms=1200, first=True)
    controller.speaker_policy.boundary("resume", at=2, reason="smart_turn")

    asyncio.run(RealtimeController._resume(controller, current))

    assert ws.generation == 4
    assert ws.speaker_mode == "filter"
    assert controller.speaker_policy.turns["resume"] is turn and turn.start_sample == 320
    assert turn.admitted and turn.boundary_at is None and turn.boundary_revision == 2
    assert controller.speaker_policy.continuity.normal_end_at is None
    router_reset.assert_called_once_with(turn_id="resume", generation=4)
    submit.assert_not_called() # 不再产生第二条固定窗口声纹流。
    controller._detail.assert_awaited_once_with("turn.resumed")
