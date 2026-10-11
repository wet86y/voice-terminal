from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from voice_terminal.config import AppConfig, SpeakerConfig
from voice_terminal.speaker.engine import SpeakerModelError
from voice_terminal.speaker.factory import CAMPPLUS_MODEL_ID, create_speaker_engine


def _tone_pcm(seconds: float, sample_rate: int = 16000) -> bytes:
    count = int(round(seconds * sample_rate))
    t = np.arange(count, dtype=np.float64) / sample_rate
    signal = 0.25 * np.sin(2 * np.pi * 190 * t) + 0.13 * np.sin(2 * np.pi * 720 * t)
    return np.clip(signal * 32767, -32768, 32767).astype("<i2").tobytes()


def test_speaker_model_path_selects_each_export_filename(tmp_path: Path) -> None:
    wespeaker = AppConfig(model_dir=tmp_path).speaker_model_path
    campplus = AppConfig(
        model_dir=tmp_path,
        speaker=SpeakerConfig(model_id=CAMPPLUS_MODEL_ID),
    ).speaker_model_path
    assert wespeaker == tmp_path / "speaker" / "wespeaker-cnceleb-resnet34" / "cnceleb_resnet34.onnx"
    assert campplus == tmp_path / "speaker" / CAMPPLUS_MODEL_ID / "campplus.onnx"


def test_factory_rejects_unknown_speaker_model_id(tmp_path: Path) -> None:
    with pytest.raises(SpeakerModelError, match="unsupported speaker model_id"):
        create_speaker_engine("unknown-speaker-model", tmp_path / "model.onnx")


@pytest.mark.parametrize("seconds", [0.75, 1.0, 2.0, 3.0])
def test_campplus_cpu_embedding_is_finite_192d_unit_vector(seconds: float) -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("torchaudio")
    model_path = Path(__file__).resolve().parents[1] / ".local" / "models" / "speaker" / CAMPPLUS_MODEL_ID / "campplus.onnx"
    if not model_path.is_file():
        pytest.skip("optional CAM++ ONNX asset has not been prepared")

    engine = create_speaker_engine(CAMPPLUS_MODEL_ID, model_path, device="cpu")
    engine.load()
    embedding = np.asarray(engine.embed_pcm(_tone_pcm(seconds)), dtype=np.float32)

    assert engine.device == "cpu"
    assert embedding.shape == (192,)
    assert np.isfinite(embedding).all()
    assert np.linalg.norm(embedding) == pytest.approx(1.0, abs=1e-5)


def test_real_worker_loads_campplus_and_emits_short_and_off_grid_windows() -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("torchaudio")
    from voice_terminal.speaker.protocol import encode_pcm
    from voice_terminal.speaker.worker import SpeakerWorkerEngine

    model_path = Path(__file__).resolve().parents[1] / ".local" / "models" / "speaker" / CAMPPLUS_MODEL_ID / "campplus.onnx"
    if not model_path.is_file():
        pytest.skip("optional CAM++ ONNX asset has not been prepared")

    worker = SpeakerWorkerEngine(
        model_path,
        sample_rate=16000,
        window_ms=2000,
        step_ms=500,
        min_speech_ms=700,
        min_window_speech_ratio=0.40,
        model_id=CAMPPLUS_MODEL_ID,
        device="cpu",
    )
    worker.load()
    assert worker.model.loaded
    assert worker.model.frontend_version == "3d-speaker-kaldi-fbank-povey-cmn-v1"

    short_samples = 12_000  # 750 ms（毫秒）@ 16 kHz（赫兹）
    short_pcm = _tone_pcm(0.75)
    assert len(short_pcm) // 2 == short_samples
    assert worker.feed({
        "turn_id": "campplus-short-tail",
        "generation": 1,
        "start_sample": 0,
        "end_sample": short_samples,
        "pcm_base64": encode_pcm(short_pcm),
        "speech": True,
    }) == []
    [short_observation] = worker.finish("campplus-short-tail", 1)

    assert (short_observation["window_started_sample"], short_observation["window_ended_sample"]) == (0, short_samples)
    assert short_observation["speech_ms"] == 750
    assert short_observation["speech_ratio"] == 1.0
    assert short_observation["quality"] == "ready"
    short_embedding = np.asarray(short_observation["embedding"], dtype=np.float32)
    assert short_embedding.shape == (192,)
    assert np.isfinite(short_embedding).all()
    assert np.linalg.norm(short_embedding) == pytest.approx(1.0, abs=1e-5)

    off_grid_samples = 36_800  # 2.3 s（秒）；末窗从样本4800（300ms）开始。
    off_grid_pcm = _tone_pcm(2.3)
    assert len(off_grid_pcm) // 2 == off_grid_samples
    emitted = worker.feed({
        "turn_id": "campplus-first-and-tail",
        "generation": 2,
        "start_sample": 0,
        "end_sample": off_grid_samples,
        "pcm_base64": encode_pcm(off_grid_pcm),
        "speech": True,
    })
    emitted.extend(worker.finish("campplus-first-and-tail", 2))

    assert [(item["window_started_sample"], item["window_ended_sample"]) for item in emitted] == [
        (0, 32_000),
        (4_800, 36_800),
    ]
    assert [item["speech_ms"] for item in emitted] == [2000, 2000]
    assert [item["speech_ratio"] for item in emitted] == [1.0, 1.0]
    assert all(item["quality"] == "ready" for item in emitted)
    for item in emitted:
        embedding = np.asarray(item["embedding"], dtype=np.float32)
        assert embedding.shape == (192,)
        assert np.isfinite(embedding).all()
        assert np.linalg.norm(embedding) == pytest.approx(1.0, abs=1e-5)
