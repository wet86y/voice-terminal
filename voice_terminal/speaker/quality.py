from __future__ import annotations


def base_window_quality(pcm: bytes, *, sample_rate: int,
                        min_speech_ms: int,
                        vad_measured: bool = False) -> tuple[str, int]:
    """检查音频窗的基础质量，返回质量标签和能量估算的人声毫秒数。

    实时 Worker（实时声纹工作进程）与现场测试共用此函数，避免静音、
    过低电平或削波音频在两条路径里得到不同的质量判断。
    vad_measured（是否已由语音活动检测测得人声时长）为真时，能量阈值只作
    后备估值，不再作为人声时长门槛；窗口时长由语音活动检测结果判定。
    """
    try:
        import numpy as np  # type: ignore

        values = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if values.size == 0:
            return "insufficient_audio", 0
        rms = float(np.sqrt(np.mean((values / 32768.0) ** 2)))
        if rms < 0.003:
            return "too_quiet", 0
        clipped = float(np.mean(np.abs(values) >= 32760.0))
        if clipped > 0.02:
            return "clipped", int(values.size * 1000 / sample_rate)
        voiced = int(np.count_nonzero(np.abs(values) >= 327.0))
        speech_ms = int(voiced * 1000 / sample_rate)
        if not vad_measured and speech_ms < min_speech_ms:
            return "insufficient_audio", speech_ms
        return "ready", speech_ms
    except Exception:
        # Preserve the existing dependency-light fallback when numpy is absent
        # or an unexpected audio representation reaches this optional service.
        return "ready", len(pcm) // 2 * 1000 // sample_rate
