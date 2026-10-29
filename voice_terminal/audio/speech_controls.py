"""Streaming PCM controls（流式音频调节）：保音高变速、增益及实际位置记录。"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import threading


@lru_cache(maxsize=1)
def tempo_available() -> bool:
    """只核对小型动态库可加载性；缺失时不阻断其他语音能力。"""
    try:
        from voice_audio_dsp import available
        return bool(available())
    except (ImportError, OSError, RuntimeError):
        return False


@dataclass(frozen=True)
class AudioTransformSpan:
    input_start: int  # input_start（源音频开始帧）。
    input_end: int  # input_end（源音频结束帧）。
    output_start: int  # output_start（实际输出开始帧）。
    output_end: int  # output_end（实际输出结束帧）。


class PcmControlProcessor:
    """每个独立合成段一个处理器，网络分块之间保留算法缓冲。"""

    def __init__(self, sample_rate: int, *, speed: float = 1, volume: float = 1,
                 channels: int = 1, tempo_factory=None) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.speed = speed
        self.volume = volume
        self.input_frames = 0
        self.output_frames = 0
        self.finished = False
        self._lock = threading.RLock()
        self._tempo = None
        if speed != 1:
            if tempo_factory is None:
                from voice_audio_dsp import TempoProcessor
                tempo_factory = TempoProcessor
            self._tempo = tempo_factory(sample_rate, channels=channels, speed=speed)

    def _output(self, pcm: bytes) -> bytes:
        if self.volume != 1 and pcm:
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            pcm = np.clip(np.rint(samples * self.volume), -32768, 32767).astype("<i2").tobytes()
        self.output_frames += len(pcm) // (2 * self.channels)
        return pcm

    def push(self, pcm: bytes) -> bytes:
        with self._lock:
            if self.finished:
                raise RuntimeError("音频调节段已经结束。")
            if len(pcm) % (2 * self.channels):
                raise ValueError("PCM（脉冲编码音频）未对齐完整采样帧。")
            self.input_frames += len(pcm) // (2 * self.channels)
            return self._output(self._tempo.push(pcm) if self._tempo else pcm)

    def finish(self) -> bytes:
        with self._lock:
            if self.finished:
                return b""
            self.finished = True
            try:
                return self._output(self._tempo.finish() if self._tempo else b"")
            finally:
                if self._tempo:
                    self._tempo.close()

    def close(self) -> None:
        """取消时丢弃尚未输出的尾缓冲，绝不把旧音频送入后继事务。"""
        with self._lock:
            self.finished = True
            if self._tempo:
                self._tempo.close()

    @property
    def buffered_input_frames(self) -> int:
        """算法持有的源帧，不混入已经交给播放器的实际输出量。"""
        with self._lock:
            return int(getattr(self._tempo, "buffered_input_frames", 0)) if not self.finished else 0

    def span(self, *, input_start: int = 0, output_start: int = 0) -> AudioTransformSpan:
        return AudioTransformSpan(input_start, input_start + self.input_frames,
                                  output_start, output_start + self.output_frames)
