"""声纹回溯起点补送：保持真实窗口、采样边界和轮次代次。"""
import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace
from voice_terminal.speaker.protocol import encode_pcm
from voice_terminal.speaker.worker import SpeakerWorkerEngine


def make_frame(start, samples=320):
    """生成 20ms PCM（脉冲编码音频）帧；不用于验证模型准确率。"""
    return AudioFrame(b"\xe8\x03" * samples, start, 1.0)


def test_backdated_onset_reaches_worker_as_one_complete_window():
    worker = SpeakerWorkerEngine(Path("unused.onnx"))
    worker.model = SimpleNamespace(model_version="test", embed_pcm=lambda _: (1.0, 0.0))
    observations = []

    def submit_frame(**kwargs):
        payload = {key: value for key, value in kwargs.items() if key != "pcm"}
        payload["pcm_base64"] = encode_pcm(kwargs["pcm"])
        observations.extend(worker.feed(payload))
        return True

    controller = object.__new__(RealtimeController)
    controller.speaker_client = SimpleNamespace(submit_frame=submit_frame, degraded=False)
    controller.state = SimpleNamespace(set_service=Mock())
    controller._speaker_turn_start_sample = 3200
    controller._recent = deque(make_frame(start) for start in range(0, 35200, 320))
    current = controller._recent[-1]
    ws = TurnWorkspace("onset", generation=2, speaker_mode="filter")

    controller._prime_speaker_turn_audio(ws, current)
    submit_frame(turn_id=ws.turn_id, generation=ws.generation, pcm=current.pcm,
                 start_sample=current.start_sample, end_sample=current.end_sample, speech=True)

    assert len(observations) == 1
    observation = observations[0]
    assert observation["window_started_sample"] == 3200
    assert observation["window_ended_sample"] == 35200
    assert observation["speech_ms"] == observation["turn_speech_ms"] == 2000
    assert observation["quality"] == "ready"


def test_preroll_clips_previous_speaker_and_excludes_current_frame():
    submit = Mock(return_value=True)
    controller = object.__new__(RealtimeController)
    controller.speaker_client = SimpleNamespace(submit_frame=submit, degraded=False)
    controller._speaker_turn_start_sample = 480
    controller._recent = deque(make_frame(start) for start in (0, 320, 640, 960))
    ws = TurnWorkspace("resumed", generation=4, speaker_mode="observe")

    controller._prime_speaker_turn_audio(ws, controller._recent[-1])

    sent = [call.kwargs for call in submit.call_args_list]
    assert [(item["start_sample"], item["end_sample"]) for item in sent] == [(480, 640), (640, 960)]
    assert len(sent[0]["pcm"]) == 160 * 2
    assert all(item["turn_id"] == "resumed" and item["generation"] == 4 for item in sent)


def test_preroll_accepts_sample_zero_and_preserves_off_mode():
    submit = Mock(return_value=True)
    controller = object.__new__(RealtimeController)
    controller.speaker_client = SimpleNamespace(submit_frame=submit, degraded=False)
    controller._speaker_turn_start_sample = 0
    controller._recent = deque((make_frame(0), make_frame(320)))
    current = controller._recent[-1]
    ws = TurnWorkspace("zero", speaker_mode="filter")
    controller._prime_speaker_turn_audio(ws, current)
    assert submit.call_args.kwargs["start_sample"] == 0
    submit.reset_mock()
    ws.speaker_mode = "off"
    controller._prime_speaker_turn_audio(ws, current)
    submit.assert_not_called()


def test_preroll_overflow_downgrades_service_and_stops_replay():
    submit = Mock(return_value=False)
    controller = object.__new__(RealtimeController)
    controller.speaker_client = SimpleNamespace(submit_frame=submit, degraded=True)
    controller.state = SimpleNamespace(set_service=Mock())
    controller._speaker_turn_start_sample = 0
    controller._recent = deque(make_frame(start) for start in (0, 320, 640))
    ws = TurnWorkspace("overflow", speaker_mode="filter")
    controller._prime_speaker_turn_audio(ws, controller._recent[-1])
    submit.assert_called_once()
    controller.state.set_service.assert_called_once_with("speaker", "DEGRADED")


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

    asyncio.run(RealtimeController._resume(controller, current))

    assert ws.generation == 4
    assert ws.speaker_mode == "filter"
    router_reset.assert_called_once_with(turn_id="resume", generation=4)
    submit.assert_called_once()
    submitted = submit.call_args.kwargs
    assert (submitted["start_sample"], submitted["end_sample"]) == (320, 640)
    assert submitted["turn_id"] == "resume"
    assert submitted["generation"] == 4
    assert submitted["speech"] is True
    controller._detail.assert_awaited_once_with("turn.resumed")
