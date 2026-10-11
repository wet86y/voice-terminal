from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")
torchaudio = pytest.importorskip("torchaudio")

from voice_terminal.speaker.features import (  # noqa: E402
    CAMPLUS_FRONTEND_VERSION,
    FRONTEND_VERSION,
    InsufficientAudioError,
    pcm_to_campplus_fbank,
)


def _tone_pcm(seconds: float, sample_rate: int = 16000) -> bytes:
    count = int(round(seconds * sample_rate))
    t = np.arange(count, dtype=np.float64) / sample_rate
    signal = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.12 * np.sin(2 * np.pi * 830 * t)
    return np.clip(signal * 32767, -32768, 32767).astype("<i2").tobytes()


@pytest.mark.parametrize("seconds", [0.75, 1.0, 2.0, 3.0])
def test_campplus_frontend_matches_official_torchaudio_fbank(seconds: float) -> None:
    pcm = _tone_pcm(seconds)
    actual = pcm_to_campplus_fbank(pcm)
    waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).unsqueeze(0)
    expected = torchaudio.compliance.kaldi.fbank(
        waveform,
        num_mel_bins=80,
        sample_frequency=16000,
        dither=0.0,
    )
    expected = expected - expected.mean(dim=0, keepdim=True)
    reference = expected.cpu().numpy()

    expected_frames = 1 + (round(seconds * 16000) - 400) // 160
    assert actual.shape == (expected_frames, 80)
    np.testing.assert_array_equal(actual, reference)
    assert np.isfinite(actual).all()


def test_campplus_frontend_identity_is_separate_from_wespeaker() -> None:
    assert CAMPLUS_FRONTEND_VERSION != FRONTEND_VERSION


def test_campplus_frontend_rejects_too_few_frames_for_statistics_pooling() -> None:
    with pytest.raises(InsufficientAudioError, match="3帧"):
        pcm_to_campplus_fbank(_tone_pcm(0.04))


def test_campplus_frontend_accepts_minimum_finite_pooling_length() -> None:
    features = pcm_to_campplus_fbank(_tone_pcm(0.045))
    assert features.shape == (3, 80)
