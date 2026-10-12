from __future__ import annotations

import unittest

import numpy as np

from voice_terminal.speaker.features import (
    FRONTEND_VERSION,
    InsufficientAudioError,
    kaldi_mel_banks,
    min_audio_samples,
    pcm_to_fbank,
    probe_pcm,
)

SR = 16000


def synthetic_pcm(seconds: float = 3.0, gain: float = 0.35, seed: int = 11) -> bytes:
    """Deterministic speech-like signal: a few formants plus noise."""
    rng = np.random.default_rng(seed)
    count = int(SR * seconds)
    t = np.arange(count, dtype=np.float64) / SR
    signal = (
        0.9 * np.sin(2 * np.pi * 180 * t)
        + 0.5 * np.sin(2 * np.pi * 700 * t + 0.3)
        + 0.3 * np.sin(2 * np.pi * 1500 * t)
        + 0.2 * np.sin(2 * np.pi * 2600 * t + 1.1)
    )
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
    signal = signal * envelope + 0.05 * rng.standard_normal(count)
    signal = signal / max(1e-9, np.abs(signal).max()) * gain
    return np.clip(signal * 32767.0, -32768, 32767).astype("<i2").tobytes()


class FrontendContractTests(unittest.TestCase):
    def test_rejects_audio_below_the_model_pooling_floor(self) -> None:
        minimum = min_audio_samples(SR)
        self.assertEqual(minimum, 1680)  # 105 ms -> 9 frames
        with self.assertRaises(InsufficientAudioError):
            pcm_to_fbank(np.zeros(minimum - 1, dtype="<i2").tobytes())
        # Exactly the minimum is accepted and yields the 9 frames the graph needs.
        self.assertEqual(pcm_to_fbank(np.zeros(minimum, dtype="<i2").tobytes()).shape[0], 9)

    def test_rejects_odd_and_empty_payloads(self) -> None:
        with self.assertRaises(ValueError):
            pcm_to_fbank(b"\x00")
        with self.assertRaises(InsufficientAudioError):
            pcm_to_fbank(b"")

    def test_target_frames_padding_is_gone(self) -> None:
        with self.assertRaises(ValueError):
            pcm_to_fbank(synthetic_pcm(1.0), target_frames=200)

    def test_mel_bank_matches_kaldi_layout(self) -> None:
        banks = kaldi_mel_banks(80, 512, SR)
        self.assertEqual(banks.shape, (80, 257))
        # Kaldi builds the triangles over n_fft/2 bins and appends one zero
        # column for the Nyquist bin; the librosa convention would use 257 bins.
        self.assertTrue(np.allclose(banks[:, -1], 0.0))
        # Unit-height triangles, never area-normalised.  With 80 bands over 256
        # FFT bins most peaks fall *between* bins, so the sampled maximum is
        # below 1 for some bands - that is Kaldi's behaviour, not a defect.
        self.assertLessEqual(float(banks.max()), 1.0 + 1e-6)
        self.assertGreater(float(np.median(banks.max(axis=1))), 0.9)

    def test_probe_is_deterministic(self) -> None:
        self.assertEqual(probe_pcm(SR), probe_pcm(SR))
        self.assertEqual(len(probe_pcm(SR)), SR * 2 * 2)
        self.assertIn("kaldi-fbank", FRONTEND_VERSION)

    def test_cmn_removes_recording_gain(self) -> None:
        """A pure level change must not move the features (why CMN matters)."""
        base = synthetic_pcm(2.0, gain=0.3)
        reference = pcm_to_fbank(base)
        for gain in (0.15, 0.6):
            candidate = pcm_to_fbank(synthetic_pcm(2.0, gain=gain))
            count = min(reference.shape[0], candidate.shape[0])
            delta = np.abs(reference[:count] - candidate[:count])
            # The bulk of the matrix is invariant; the two narrowest
            # low-frequency bands keep a small residual because preemphasis
            # attenuates their single FFT bin by ~28 dB.  torchaudio's own
            # reference shows the same residual (0.4195 vs 0.4183 here).
            self.assertLess(float(delta.mean()), 5e-3)
            self.assertLess(float(np.percentile(delta, 99)), 2e-2)
        # Without CMN the same level change is a large constant offset of
        # exactly 2*ln(2) in the log domain.
        raw_base = pcm_to_fbank(base, cmn=False)
        raw_quiet = pcm_to_fbank(synthetic_pcm(2.0, gain=0.15), cmn=False)
        self.assertGreater(float(np.abs(raw_base - raw_quiet).mean()), 1.0)


class FrontendReferenceTests(unittest.TestCase):
    """Compare against torchaudio's Kaldi implementation when available."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import torch  # noqa: F401
            import torchaudio  # noqa: F401
        except Exception:  # pragma: no cover - torchaudio is not a runtime dep
            raise unittest.SkipTest("torchaudio unavailable")

    def reference(self, pcm: bytes, *, cmn: bool = True) -> np.ndarray:
        import torch
        import torchaudio

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        waveform = torch.from_numpy(samples / 32768.0).unsqueeze(0) * (1 << 15)
        features = torchaudio.compliance.kaldi.fbank(
            waveform,
            num_mel_bins=80,
            frame_length=25.0,
            frame_shift=10.0,
            dither=0.0,
            sample_frequency=16000,
            window_type="hamming",
            use_energy=False,
        ).numpy()
        return features - features.mean(axis=0) if cmn else features

    def test_mel_bank_matches_reference_matrix(self) -> None:
        import torch
        import torchaudio

        reference, _ = torchaudio.compliance.kaldi.get_mel_banks(
            80, 512, 16000.0, 20.0, 0.0, 100.0, -500.0, 1.0)
        reference = torch.nn.functional.pad(reference, (0, 1), mode="constant", value=0).numpy()
        self.assertLess(float(np.abs(kaldi_mel_banks(80, 512, SR) - reference).max()), 1e-5)

    def test_features_match_weSpeaker_reference(self) -> None:
        # Tight mean/p99 agreement everywhere, with a looser absolute bound on
        # the very narrowest low bands: torchaudio evaluates in float32 while
        # this frontend uses float64, and preemphasis attenuates the 62.5 Hz
        # bin of band 1 by ~28 dB, so a float32 rounding there is worth ~2.6e-3
        # in the log domain.  This implementation is the more accurate side.
        for seconds, gain in ((2.0, 0.3), (5.0, 0.6), (1.2, 0.15)):
            pcm = synthetic_pcm(seconds, gain=gain)
            got = pcm_to_fbank(pcm)
            want = self.reference(pcm)
            self.assertEqual(got.shape, want.shape)
            delta = np.abs(got - want)
            label = f"{seconds}s gain={gain}"
            self.assertLess(float(delta.mean()), 1e-5, label)
            self.assertLess(float(np.percentile(delta, 99)), 1e-3, label)
            self.assertLess(float(delta.max()), 5e-3, label)

    def test_frame_count_uses_snip_edges(self) -> None:
        pcm = synthetic_pcm(1.0)
        samples = len(pcm) // 2
        expected = 1 + (samples - 400) // 160
        self.assertEqual(pcm_to_fbank(pcm).shape[0], expected)
        self.assertEqual(self.reference(pcm).shape[0], expected)


class FrontendEmbeddingTests(unittest.TestCase):
    """End-to-end: the frontend must be indistinguishable to the model."""

    def setUp(self) -> None:
        from pathlib import Path

        from voice_terminal.config import load_config, SpeakerConfig

        try:
            import onnxruntime  # noqa: F401
        except Exception:  # pragma: no cover
            raise unittest.SkipTest("onnxruntime unavailable")
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config" / "default.yaml", root / "config" / "local.yaml")
        config = __import__("dataclasses").replace(config, speaker=SpeakerConfig(model_id="wespeaker-cnceleb-resnet34"))
        if not config.speaker_model_path.is_file():
            raise unittest.SkipTest("speaker model not installed")
        from voice_terminal.speaker.engine import WeSpeakerOnnxEngine

        self.engine = WeSpeakerOnnxEngine(config.speaker_model_path)
        self.engine.load()

    def _reference_embedding(self, pcm: bytes) -> np.ndarray:
        import torch
        import torchaudio

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        waveform = torch.from_numpy(samples / 32768.0).unsqueeze(0) * (1 << 15)
        features = torchaudio.compliance.kaldi.fbank(
            waveform, num_mel_bins=80, frame_length=25.0, frame_shift=10.0,
            dither=0.0, sample_frequency=16000, window_type="hamming", use_energy=False).numpy()
        features = features - features.mean(axis=0)
        out = self.engine.session.run(
            [self.engine.output_name],
            {self.engine.input_name: features[None, :, :]})[0]
        vector = np.asarray(out, dtype=np.float32).reshape(-1)
        return vector / np.linalg.norm(vector)

    def test_embeddings_match_reference_pipeline(self) -> None:
        pcm = synthetic_pcm(4.0, gain=0.4)
        mine = np.asarray(self.engine.embed_pcm(pcm), dtype=np.float32)
        theirs = self._reference_embedding(pcm)
        self.assertGreater(float(mine @ theirs), 0.9999)

    def test_probe_embedding_is_stable_and_change_sensitive(self) -> None:
        first = self.engine.probe_embedding()
        self.assertEqual(first, self.engine.probe_embedding())
        self.assertEqual(len(first), self.engine.embedding_dim or len(first))
        # A different signal must produce a clearly different embedding,
        # otherwise the fingerprint would not detect a frontend change.
        other = np.asarray(self.engine.embed_pcm(synthetic_pcm(2.0, gain=0.4, seed=99)),
                           dtype=np.float32)
        self.assertLess(float(np.asarray(first) @ other), 0.99)


if __name__ == "__main__":
    unittest.main()
