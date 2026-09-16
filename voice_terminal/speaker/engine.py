from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class SpeakerModelError(RuntimeError):
    pass


def _hz_to_mel(value: Any) -> float:
    return 1127.0 * float(np.log1p(float(value) / 700.0))


def _mel_to_hz(value: Any) -> Any:
    return 700.0 * (np.expm1(np.asarray(value, dtype=np.float32) / 1127.0))


def _mel_filterbank(sample_rate: int = 16000, n_fft: int = 512, n_mels: int = 80) -> Any:
    if np is None:
        raise SpeakerModelError("numpy unavailable")
    low_mel = _hz_to_mel(20.0)
    high_mel = _hz_to_mel(sample_rate / 2.0)
    points = _mel_to_hz(np.linspace(low_mel, high_mel, n_mels + 2))
    bins = np.floor((n_fft + 1) * points / sample_rate).astype(np.int32)
    bank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for index in range(n_mels):
        left, center, right = int(bins[index]), int(bins[index + 1]), int(bins[index + 2])
        if center > left:
            bank[index, left:center] = np.arange(left, center, dtype=np.float32) - left
            bank[index, left:center] /= float(center - left)
        if right > center:
            bank[index, center:right] = right - np.arange(center, right, dtype=np.float32)
            bank[index, center:right] /= float(right - center)
    return bank


def pcm_to_fbank(pcm: bytes, *, sample_rate: int = 16000, num_mels: int = 80,
                 target_frames: int | None = None) -> Any:
    """Small dependency-light Kaldi-style log Mel feature approximation."""
    if np is None:
        raise SpeakerModelError("numpy unavailable")
    if len(pcm) % 2:
        raise SpeakerModelError("PCM must contain complete int16 samples")
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        raise SpeakerModelError("empty audio")
    frame_length = max(1, round(sample_rate * 0.025))
    frame_shift = max(1, round(sample_rate * 0.010))
    if samples.size < frame_length:
        samples = np.pad(samples, (0, frame_length - samples.size))
    frame_count = 1 + max(0, (samples.size - frame_length) // frame_shift)
    required = (frame_count - 1) * frame_shift + frame_length
    if required > samples.size:
        samples = np.pad(samples, (0, required - samples.size))
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length)[::frame_shift]
    window = np.hamming(frame_length).astype(np.float32)
    spectrum = np.fft.rfft(frames * window, n=512, axis=1)
    power = (np.abs(spectrum) ** 2).astype(np.float32)
    bank = _mel_filterbank(sample_rate, 512, num_mels)
    features = np.log(np.maximum(power @ bank.T, 1e-10)).astype(np.float32)
    # The exported WeSpeaker models accept B x T x 80.  Pad fixed-shape models
    # with the per-utterance mean rather than zeros, keeping the feature scale
    # close to the valid speech frames.
    if target_frames is not None and target_frames > 0:
        if features.shape[0] < target_frames:
            mean = features.mean(axis=0, keepdims=True)
            features = np.concatenate([features, np.repeat(mean, target_frames - features.shape[0], axis=0)], axis=0)
        elif features.shape[0] > target_frames:
            features = features[-target_frames:]
    return features


class WeSpeakerOnnxEngine:
    """Optional ONNX embedding engine; importing it never makes the runtime mandatory."""

    def __init__(self, model_path: Path, *, sample_rate: int = 16000, model_version: str = "") -> None:
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.model_version = model_version or model_path.stem
        self.session: Any = None
        self.input_name = "feats"
        self.output_name = "embs"
        self.target_frames: int | None = 200
        self.input_layout = "btf"
        self.error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.session is not None and self.error is None

    def load(self) -> None:
        if np is None:
            self.error = "numpy unavailable"
            raise SpeakerModelError(self.error)
        if not self.model_path.is_file():
            self.error = f"model not found: {self.model_path}"
            raise SpeakerModelError(self.error)
        try:
            import onnxruntime as ort  # type: ignore
            self.session = ort.InferenceSession(
                str(self.model_path),
                providers=["CPUExecutionProvider"],
            )
            inputs = self.session.get_inputs()
            if not inputs:
                raise SpeakerModelError("ONNX model has no inputs")
            self.input_name = str(inputs[0].name)
            shape = inputs[0].shape
            if len(shape) >= 3:
                # Different WeSpeaker exports use either B x T x 80 or
                # B x 80 x T.  Detect the static feature axis and adapt at
                # inference time; dynamic dimensions retain the documented
                # 200-frame (roughly two-second) fallback.
                if shape[1] == 80 and shape[2] != 80:
                    self.input_layout = "bft"
                    if isinstance(shape[2], int) and shape[2] > 0:
                        self.target_frames = int(shape[2])
                    else:
                        # A dynamic time axis can consume the complete
                        # enrollment/observation window.  Do not silently
                        # truncate it to the two-second fallback used only
                        # for exports with no shape metadata.
                        self.target_frames = None
                elif shape[2] == 80:
                    self.input_layout = "btf"
                    if isinstance(shape[1], int) and shape[1] > 0:
                        self.target_frames = int(shape[1])
                    else:
                        self.target_frames = None
                elif isinstance(shape[1], int) and shape[1] > 0:
                    self.target_frames = int(shape[1])
                else:
                    self.target_frames = None
            outputs = self.session.get_outputs()
            if outputs:
                self.output_name = str(outputs[0].name)
            self.error = None
        except Exception as exc:
            self.session = None
            self.error = f"WeSpeaker load failed: {type(exc).__name__}: {exc}"
            raise SpeakerModelError(self.error) from exc

    def embed_pcm(self, pcm: bytes) -> tuple[float, ...]:
        if not self.loaded:
            raise SpeakerModelError(self.error or "model not loaded")
        features = pcm_to_fbank(
            pcm, sample_rate=self.sample_rate, target_frames=self.target_frames,
        )
        model_features = features.T[None, :, :] if self.input_layout == "bft" else features[None, :, :]
        result = self.session.run([self.output_name], {self.input_name: model_features})
        if not result:
            raise SpeakerModelError("ONNX model returned no embedding")
        embedding = np.asarray(result[0], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(embedding))
        if not embedding.size or not np.isfinite(embedding).all() or not np.isfinite(norm) or norm <= 1e-8:
            raise SpeakerModelError("ONNX model returned an invalid embedding")
        return tuple(float(x) for x in (embedding / norm))

    @staticmethod
    def similarity(first: tuple[float, ...], second: tuple[float, ...]) -> float:
        if np is None or not first or not second or len(first) != len(second):
            return -1.0
        a = np.asarray(first, dtype=np.float32)
        b = np.asarray(second, dtype=np.float32)
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return -1.0
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 1e-8:
            return -1.0
        cosine = float(np.dot(a, b) / denom)
        # WeSpeaker verification thresholds are calibrated against cosine
        # similarity itself (identical vectors=1, orthogonal=0, opposite=-1).
        # Do not shift this raw score into [0, 1], which would make the 0.50
        # product baseline equivalent to accepting every non-negative pair.
        return max(-1.0, min(1.0, cosine))
