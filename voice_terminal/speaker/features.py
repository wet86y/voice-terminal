"""WeSpeaker-compatible log-Mel frontend.

This module is the single source of truth for turning 16 kHz int16 PCM into the
80-dim feature matrix that ``cnceleb_resnet34.onnx`` was trained on.  It is a
dependency-light (numpy only) reimplementation of WeSpeaker's official
``compute_fbank``::

    waveform = waveform * (1 << 15)
    mat = kaldi.fbank(waveform, num_mel_bins=80, frame_length=25,
                      frame_shift=10, dither=dither,
                      sample_frequency=sample_rate,
                      window_type='hamming', use_energy=False)
    mat = mat - np.mean(mat, axis=0)          # CMN, without CVN

Everything that follows is derived from that reference and validated against
``torchaudio.compliance.kaldi.fbank`` in ``tests/test_speaker_features.py``:

* 25 ms / 10 ms frames, ``snip_edges=True``  -> ``1 + (n - 400) // 160`` frames
* per-frame DC offset removal, then preemphasis 0.97 (replicate at sample 0)
* Hamming window with ``periodic=False`` (``0.54 - 0.46 * cos(2*pi*n/(N-1))``)
* zero padding to the next power of two (512) before ``rfft``
* Kaldi mel filterbank: triangles defined in the MEL domain, peak 1, no
  normalisation, built over ``n_fft / 2`` FFT bins with one zero column appended
* natural log with the float32 epsilon floor
* CMN over time (mean removal only, never variance)

The previous hand-rolled approximation omitted CMN, preemphasis and DC removal,
used a librosa-style linear-frequency triangle bank indexed with ``n_fft + 1``,
and did not apply the ``2**15`` amplitude scaling.  Measured against the
reference that cost ~110% mean relative feature error and made the embedding
track the recording channel instead of the speaker.
"""

from __future__ import annotations

import numpy as np

#: Semantic version of the frontend.  Any change here (or in the model file)
#: must invalidate previously enrolled profiles; see
#: :meth:`voice_terminal.speaker.registry.SpeakerProfileStore.load`.
FRONTEND_VERSION = "kaldi-fbank-hamming-cmn-v2"

_EPS32 = float(np.finfo(np.float32).eps)

# WeSpeaker's ONNX graph reduces with an unbiased variance over the pooled time
# axis, so a padded time length below this value divides by zero and the model
# silently returns NaN instead of raising.
_MIN_POOLED_FRAMES = 9


class InsufficientAudioError(ValueError):
    """Audio is too short for the speaker model to produce a valid embedding."""


def min_audio_samples(sample_rate: int = 16000) -> int:
    """Shortest PCM length (samples) that yields a non-degenerate embedding."""
    frame_length = int(sample_rate * 0.025)
    frame_shift = int(sample_rate * 0.010)
    return frame_length + (_MIN_POOLED_FRAMES - 1) * frame_shift


def _hz_to_mel(value):
    return 1127.0 * np.log1p(np.asarray(value, dtype=np.float64) / 700.0)


def kaldi_mel_banks(num_mels: int = 80, n_fft: int = 512, sample_rate: int = 16000,
                    low_freq: float = 20.0, high_freq: float = 0.0) -> np.ndarray:
    """Kaldi ``MelBanks``: mel-domain triangles of unit peak, no normalisation.

    ``high_freq <= 0`` means "offset from Nyquist", matching Kaldi.  The matrix
    is built over ``n_fft // 2`` FFT bins and one zero column is appended so it
    lines up with ``np.fft.rfft``'s ``n_fft // 2 + 1`` outputs.  Building the
    triangles over ``n_fft + 1`` bins instead (the librosa convention) is a
    real, measurable deviation from the official frontend.

    The arithmetic matches torchaudio's ``get_mel_banks``: only the mel
    break-point scalars are float64, everything else is computed in float32.
    Doing it in float64 shifts the triangle edges by ~1e-5, which is enough to
    change the two narrowest low-frequency bands by up to 2.6e-3 in the log
    domain.
    """
    num_fft_bins = n_fft // 2
    nyquist = 0.5 * sample_rate
    if high_freq <= 0.0:
        high_freq = nyquist + high_freq
    fft_bin_width = float(sample_rate) / float(n_fft)
    mel_low = float(_hz_to_mel(low_freq))
    mel_high = float(_hz_to_mel(high_freq))
    mel_delta = (mel_high - mel_low) / (num_mels + 1)
    index = np.arange(num_mels, dtype=np.float32)[:, None]
    left = np.float32(mel_low) + index * np.float32(mel_delta)
    center = np.float32(mel_low) + (index + np.float32(1.0)) * np.float32(mel_delta)
    right = np.float32(mel_low) + (index + np.float32(2.0)) * np.float32(mel_delta)
    bin_mel = (1127.0 * np.log1p(
        np.float32(fft_bin_width) * np.arange(num_fft_bins, dtype=np.float32) / np.float32(700.0)
    )).astype(np.float32)[None, :]
    up_slope = (bin_mel - left) / (center - left)
    down_slope = (right - bin_mel) / (right - center)
    banks = np.maximum(0.0, np.minimum(up_slope, down_slope)).astype(np.float32)
    return np.pad(banks, ((0, 0), (0, 1)), mode="constant")


def pcm_to_fbank(pcm: bytes, *, sample_rate: int = 16000, num_mels: int = 80,
                 cmn: bool = True, target_frames: int | None = None) -> np.ndarray:
    """Return a ``(T, num_mels)`` float32 log-Mel matrix for int16 PCM.

    ``cmn`` must stay enabled: WeSpeaker trains and evaluates with CMN, and
    without it the embedding becomes sensitive to recording gain (a pure level
    change moved a genuine score by more than 0.2 cosine in testing).

    ``target_frames`` is accepted only so old call sites fail loudly.  This
    ONNX export has a dynamic time axis, so padding to a fixed length is never
    necessary and padding with the per-utterance mean would inject constant
    frames into statistics pooling.
    """
    if target_frames is not None:
        raise ValueError(
            "target_frames padding was removed: the speaker ONNX has a dynamic "
            "time axis and mean-padding corrupts statistics pooling"
        )
    if len(pcm) % 2:
        raise ValueError("PCM must contain complete int16 samples")
    if not pcm:
        raise InsufficientAudioError("empty audio")

    # int16 sample values are already the (1 << 15) scaled waveform that
    # WeSpeaker feeds to kaldi.fbank, so no extra scaling is needed here.
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    frame_length = int(sample_rate * 0.025)
    frame_shift = int(sample_rate * 0.010)
    if frame_length <= 0 or frame_shift <= 0:
        raise ValueError("sample_rate is too low for 25 ms / 10 ms framing")
    if samples.size < frame_length:
        raise InsufficientAudioError(
            f"need at least {frame_length} samples ({frame_length * 1000 // sample_rate} ms), "
            f"got {samples.size}"
        )
    n_fft = 1
    while n_fft < frame_length:
        n_fft *= 2

    frame_count = 1 + (samples.size - frame_length) // frame_shift
    if frame_count < _MIN_POOLED_FRAMES:
        raise InsufficientAudioError(
            f"need at least {min_audio_samples(sample_rate)} samples "
            f"({min_audio_samples(sample_rate) * 1000 // sample_rate} ms) for the "
            f"pooling layer, got {samples.size}"
        )
    frames = np.lib.stride_tricks.sliding_window_view(
        samples, frame_length)[::frame_shift][:frame_count].copy()

    frames -= frames.mean(axis=1, keepdims=True)              # remove_dc_offset
    preemphasised = np.empty_like(frames)                     # preemphasis 0.97
    # torchaudio replicates the first sample at the left edge, so frame[0]
    # becomes ``x[0] - 0.97 * x[0]``.
    preemphasised[:, 0] = frames[:, 0] - 0.97 * frames[:, 0]
    preemphasised[:, 1:] = frames[:, 1:] - 0.97 * frames[:, :-1]

    window = 0.54 - 0.46 * np.cos(
        2.0 * np.pi * np.arange(frame_length) / (frame_length - 1)
    )
    padded = np.zeros((frame_count, n_fft), dtype=np.float64)
    padded[:, :frame_length] = preemphasised * window

    power = np.abs(np.fft.rfft(padded, axis=1)) ** 2
    banks = kaldi_mel_banks(num_mels, n_fft, sample_rate)
    features = np.log(np.maximum(power @ banks.T, _EPS32)).astype(np.float32)
    if cmn:
        features = features - features.mean(axis=0, keepdims=True)
    return features


def probe_pcm(sample_rate: int = 16000, seconds: float = 2.0) -> bytes:
    """A deterministic, speaker-independent probe signal.

    Used to fingerprint the frontend + model pair.  It is generated in code
    (no data file) and deliberately rich in spectral content so that any change
    to framing, windowing, the mel bank, CMN or the model weights moves the
    resulting embedding well outside floating point noise.
    """
    count = max(1, int(sample_rate * seconds))
    t = np.arange(count, dtype=np.float64) / float(sample_rate)
    sweep = 200.0 + 1500.0 * (t / max(t[-1], 1e-9))
    signal = 0.25 * np.sin(2.0 * np.pi * sweep * t)
    signal += 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    signal += 0.10 * np.sin(2.0 * np.pi * 1750.0 * t)
    rng = np.random.default_rng(0x5EED)
    signal += 0.02 * rng.standard_normal(count)
    peak = float(np.abs(signal).max())
    if peak > 0:
        signal = signal * (0.5 / peak)
    return np.clip(signal * 32767.0, -32768.0, 32767.0).astype("<i2").tobytes()
