"""验证 Worker（工作进程）与设置页测试采用一致的声纹窗口边界。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
from voice_terminal.speaker.protocol import encode_pcm
from voice_terminal.speaker.state import SpeakerStateConfig
from voice_terminal.speaker.worker import SpeakerWorkerEngine


class _RecordingEmbeddingModel:
    model_version = "fake-model"
    frontend_version = "fake-frontend"  # frontend_version（替身模型的特征前端指纹）。

    def __init__(self) -> None:
        self.inputs: list[bytes] = []

    def embed_pcm(self, pcm: bytes) -> tuple[float, float]:
        self.inputs.append(pcm)
        return (1.0, 0.0)


def _worker(model: _RecordingEmbeddingModel) -> SpeakerWorkerEngine:
    worker = SpeakerWorkerEngine(
        Path("missing.onnx"), sample_rate=16000,
        window_ms=2000, step_ms=500, min_speech_ms=700,
        min_window_speech_ratio=0.40,
    )
    worker.model = model
    return worker


def _settings_runtime(model: _RecordingEmbeddingModel):
    from voice_terminal.speaker.state import SpeakerStateMachine

    config = SpeakerStateConfig(
        confirm_threshold=0.60, continuity_threshold=0.50,
        min_speech_ms=700, min_turn_speech_ms=2000,
    )
    runtime = object.__new__(SpeakerRuntimeMixin)
    runtime.config = SimpleNamespace(
        audio=SimpleNamespace(sample_rate=16000),
        speaker=SimpleNamespace(
            window_ms=2000, step_ms=500, min_window_speech_ratio=0.40,
        ),
    )
    runtime.speaker_router = SimpleNamespace(
        state_config=config,
        score_embedding=lambda _embedding: 0.80,
        score_aggregate=lambda _aggregate: 0.80,
    )
    runtime.audio_processor = None
    runtime._get_speaker_engine = lambda: model
    runtime._speaker_speech_ms = lambda pcm: (len(pcm) // 2 * 1000 // 16000, True)
    return runtime


def _fully_voiced_ranges(_pcm: bytes, *, windows, **_kwargs):
    return [[(start, end)] for start, end in windows], True


def test_short_worker_flush_uses_actual_duration_for_speech_ratio():
    """speech_ratio（单窗人声占比）以750ms真实短窗为分母。"""
    model = _RecordingEmbeddingModel()
    worker = _worker(model)
    sample_count = 12_000  # 750 ms at 16 kHz
    pcm = (4096).to_bytes(2, "little", signed=True) * sample_count
    assert worker.feed({
        "turn_id": "short", "generation": 1,
        "start_sample": 0, "end_sample": sample_count,
        "pcm_base64": encode_pcm(pcm), "speech": True,
    }) == []

    [observation] = worker.finish("short", 1)

    assert observation["speech_ms"] == 750
    assert observation["speech_ratio"] == 1.0
    assert observation["quality"] == "ready"
    assert observation["embedding"] == [1.0, 0.0]
    assert model.inputs == [pcm]


def test_settings_test_and_worker_share_off_grid_final_window():
    """设置页测试与 Worker 对2.3秒片段都应得到首窗和偏移末窗。"""
    sample_count = 36_800  # 2.3 s at 16 kHz; the final 2 s window starts at 300 ms.
    pcm = (4096).to_bytes(2, "little", signed=True) * sample_count

    live_model = _RecordingEmbeddingModel()
    worker = _worker(live_model)
    emitted = worker.feed({
        "turn_id": "tail", "generation": 1,
        "start_sample": 0, "end_sample": sample_count,
        "pcm_base64": encode_pcm(pcm), "speech": True,
    })
    emitted.extend(worker.finish("tail", 1))

    test_model = _RecordingEmbeddingModel()
    runtime = _settings_runtime(test_model)
    with patch(
        "voice_terminal.server.speaker_runtime.voiced_ranges_for_windows",
        side_effect=_fully_voiced_ranges,
    ):
        result = SpeakerRuntimeMixin._score_speaker_audio_owned(
            runtime, pcm, speech_ms=2300,
        )

    expected = [pcm[:64_000], pcm[9_600:73_600]]
    assert [(item["window_started_sample"], item["window_ended_sample"])
            for item in emitted] == [(0, 32_000), (4_800, 36_800)]
    assert live_model.inputs == expected
    assert test_model.inputs == expected
    assert result["candidate_window_count"] == 2
    assert result["window_count"] == 2
    assert result["state"] == "owner_confirmed"
    assert [item["speech_ratio"] for item in result["window_diagnostics"]] == [1.0, 1.0]


def test_settings_short_window_is_eligible_but_turn_gate_stays_two_seconds():
    """短窗可过单窗质量门，min_turn_speech_ms（整轮人声下限）仍拒绝确认。"""
    sample_count = 12_000  # 750 ms at 16 kHz
    pcm = (4096).to_bytes(2, "little", signed=True) * sample_count
    model = _RecordingEmbeddingModel()
    runtime = _settings_runtime(model)
    with patch(
        "voice_terminal.server.speaker_runtime.voiced_ranges_for_windows",
        side_effect=_fully_voiced_ranges,
    ):
        result = SpeakerRuntimeMixin._score_speaker_audio_owned(
            runtime, pcm, speech_ms=750,
        )

    assert result["candidate_window_count"] == 1
    assert result["eligible_window_count"] == 1
    assert result["window_diagnostics"][0]["speech_ratio"] == 1.0
    assert result["window_diagnostics"][0]["quality"] == "ready"
    assert result["window_count"] == 1
    assert result["speech_ms"] == 750
    assert result["state"] == "unknown"
    assert result["reason"] == "speaker_test_insufficient_turn_speech"
    assert len(model.inputs) == 1
