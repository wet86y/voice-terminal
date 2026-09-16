from __future__ import annotations

import io
import wave

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


def decode_audio_bytes(
    value: bytes,
    *,
    target_rate: int = 16000,
    max_bytes: int = 10 * 16000 * 2,
) -> bytes:
    """Decode a short browser/WAV upload into bounded mono int16 PCM."""
    if not isinstance(value, (bytes, bytearray)) or not value:
        raise ValueError("录音为空")
    raw = bytes(value)
    # A stereo WAV has twice as many source bytes before it is downmixed to
    # the bounded mono representation; raw PCM uploads remain capped by the
    # stricter post-decode check below.
    if len(raw) > max(2, int(max_bytes)) * 2 + 64:
        raise ValueError("录音数据过大")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            channels = reader.getnchannels()
            rate = reader.getframerate()
            width = reader.getsampwidth()
            frames = reader.readframes(reader.getnframes())
        if len(frames) > max(2, int(max_bytes)) * max(1, channels):
            raise ValueError("录音数据过大")
        if channels < 1 or channels > 2 or width != 2:
            raise ValueError("仅支持 16 位单声道或双声道 WAV")
        if np is None:
            raise ValueError("numpy unavailable")
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != target_rate:
            duration = samples.size / max(1, rate)
            target_count = max(1, round(duration * target_rate))
            source_x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
            target_x = np.linspace(0.0, 1.0, target_count, endpoint=False)
            samples = np.interp(target_x, source_x, samples).astype(np.float32)
        samples = np.clip(samples, -32768, 32767).astype("<i2")
        result = samples.tobytes()
        if len(result) > max(2, int(max_bytes)):
            raise ValueError("录音数据过大")
        return result
    except (wave.Error, EOFError):
        # API clients may send raw 16 kHz PCM for local testing.
        if len(raw) % 2:
            raise ValueError("原始 PCM 必须是 16 位对齐")
        if len(raw) > max(2, int(max_bytes)):
            raise ValueError("录音数据过大")
        return raw
