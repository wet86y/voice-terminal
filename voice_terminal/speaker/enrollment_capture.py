"""Enrollment capture on the *same* signal chain the runtime uses.

The previous enrollment path recorded through the browser: a different device
selection, WebRTC echo cancellation / noise suppression / automatic gain
control all enabled, and a hand-written 48 kHz -> 16 kHz linear decimation with
no anti-alias filter.  Runtime audio instead comes from ``sounddevice`` pinned
to ``audio.input_device`` at a native 16 kHz with ``automatic_gain_control:
false``.  Because the feature frontend turns out to be channel-sensitive, that
mismatch put the enrolled templates in a different acoustic domain from the
audio they were later compared against.

This module records from the same device, at the same rate/format, and runs the
samples through the very same :class:`~voice_terminal.audio.processing.AudioPreprocessor`
instance used by the live session, so enrollment and verification see the same
kind of signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from ..audio.devices import AudioDeviceError, resolve_device

#: Shortest and longest acceptable enrollment segment.
MIN_SEGMENT_SECONDS = 5.0
MAX_SEGMENT_SECONDS = 10.0

#: Bounds on the capture request itself, independent of the guidance range.
_HARD_MIN_SECONDS = 3.0
_HARD_MAX_SECONDS = 15.0


class EnrollmentCaptureError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SPEAKER_CAPTURE_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CapturedSegment:
    pcm: bytes
    duration_ms: int
    speech_ms: int
    rms_dbfs: float
    clipped_ratio: float
    processed: bool


def _dbfs(rms: float) -> float:
    if rms <= 1e-9:
        return -120.0
    import math
    return round(20.0 * math.log10(rms), 2)


def speech_ms_for_pcm(pcm: bytes, *, sample_rate: int = 16000,
                      processor: Any = None) -> tuple[int, bool]:
    """Voiced duration of ``pcm`` in milliseconds.

    Returns ``(speech_ms, measured)``.  When an :class:`AudioPreprocessor` is
    supplied its per-10 ms WebRTC voice decision is used, which is the same
    detector the live chain runs.  Without one, no verdict is produced rather
    than falling back to a gain-dependent amplitude threshold.
    """
    if processor is None or not getattr(processor, "available", False):
        return 0, False
    frame_bytes = max(2, sample_rate // 100 * 2)  # 10 ms of mono int16
    voiced = 0
    frames = 0
    for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        processor.process(pcm[offset:offset + frame_bytes])
        verdict = getattr(processor, "last_voice_activity", None)
        if verdict is not None:
            frames += 1
            if verdict:
                voiced += 1
    if not frames:
        return 0, False
    return int(voiced * 1000 // 100), True


def capture_segment(*, sample_rate: int = 16000, channels: int = 1,
                    input_device: str | int | None = None,
                    seconds: float = 8.0, processor: Any = None,
                    block_ms: int = 20) -> CapturedSegment:
    """Record one bounded enrollment segment from the runtime input device."""
    if np is None:
        raise EnrollmentCaptureError("numpy 不可用", code="SPEAKER_CAPTURE_UNAVAILABLE")
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:
        raise EnrollmentCaptureError("缺少 sounddevice，请先运行安装脚本。",
                                     code="SPEAKER_CAPTURE_UNAVAILABLE") from exc

    duration = max(_HARD_MIN_SECONDS, min(_HARD_MAX_SECONDS, float(seconds)))
    try:
        device = resolve_device(input_device, input_device=True)
    except AudioDeviceError as exc:
        raise EnrollmentCaptureError(str(exc), code="SPEAKER_CAPTURE_DEVICE") from exc

    frames = int(round(duration * sample_rate))
    blocksize = max(1, sample_rate * block_ms // 1000)
    try:
        recording = sd.rec(frames, samplerate=sample_rate, channels=channels,
                           dtype="int16", device=device, blocking=True)
    except Exception as exc:  # pragma: no cover - hardware dependent
        raise EnrollmentCaptureError(f"麦克风采集失败：{exc}",
                                     code="SPEAKER_CAPTURE_DEVICE") from exc

    samples = np.asarray(recording, dtype=np.int16)
    if samples.ndim > 1:
        samples = samples.reshape(-1, samples.shape[-1]).mean(axis=1).astype(np.int16)
    samples = np.ascontiguousarray(samples.reshape(-1))
    raw = samples.tobytes()
    if not raw:
        raise EnrollmentCaptureError("没有采集到音频", code="SPEAKER_CAPTURE_EMPTY")

    scaled = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(scaled ** 2))) if scaled.size else 0.0
    clipped = float(np.mean(np.abs(samples) >= 32760.0)) if samples.size else 0.0

    processed = False
    pcm = raw
    if processor is not None and getattr(processor, "available", False):
        try:
            processor.reset()
            pcm = processor.process(raw) or raw
            processed = True
        except Exception:
            # A processing failure must not block enrollment; the raw capture
            # from the same device is still far closer to runtime than the
            # browser path it replaced.
            pcm = raw
            processed = False

    speech_ms, _ = speech_ms_for_pcm(pcm, sample_rate=sample_rate, processor=processor)
    if processor is not None:
        processor.reset()
    return CapturedSegment(
        pcm=pcm,
        duration_ms=int(len(pcm) // 2 * 1000 // sample_rate),
        speech_ms=int(speech_ms),
        rms_dbfs=_dbfs(rms),
        clipped_ratio=round(clipped, 4),
        processed=processed,
    )
