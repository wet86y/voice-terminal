"""Local audio device（本地音频设备）：保存同一组物理音频能力的引用。"""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time

from ..audio.capture import AudioCapture
from ..audio.devices import AudioDeviceError, resolve_device
from ..audio.player import PcmPlayer
from ..audio.processing import AudioPreprocessor
from .contracts import CapturePort, PlaybackPort, ProcessingPort, TimedCaptureControl


@dataclass
class LocalAudioDevice:
    """LocalAudioDevice（本地音频设备）：能力级恢复只替换相应引用。"""

    capture: CapturePort | None = None
    processor: ProcessingPort | None = None
    player: PlaybackPort | None = None

    def create_capture(self, **options) -> CapturePort:
        """create_capture（本机采集组装）：所有权回到设备层，参数与原驱动保持一致。"""
        return AudioCapture(**options)

    def create_processor(self, **options) -> ProcessingPort:
        """create_processor（本机前处理组装）：保留 AEC（回声消除）及降噪配置。"""
        return AudioPreprocessor(**options)

    def create_player(self, **options) -> PlaybackPort:
        """create_player（本机播放组装）：调用方持有的同一播放器实例不变。"""
        return PcmPlayer(**options)

    def record_bounded(self, *, sample_rate: int, channels: int, input_device: str | int | None,
                       seconds: float, block_ms: int, control: TimedCaptureControl) -> tuple[bytes, str]:
        """record_bounded（限时采集）：用现有默认设备规则读取有界 PCM（脉冲编码采样数据）。"""
        if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 1 <= sample_rate <= 192_000
                or isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= 8
                or isinstance(block_ms, bool) or not isinstance(block_ms, int) or not 1 <= block_ms <= 200
                or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= 15):
            control.closed.set()
            raise ValueError("限时采集格式或时长超出设备边界。")
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as exc:
            control.closed.set()
            raise RuntimeError("缺少 sounddevice（音频设备库），无法录制。") from exc
        try:
            device = resolve_device(input_device, input_device=True)
        except AudioDeviceError:
            control.closed.set()
            raise
        frames = int(round(seconds * sample_rate))
        blocksize = max(1, sample_rate * block_ms // 1000)
        chunks: list[bytes] = []
        received = 0
        complete = threading.Event()
        callback_error = ""

        def receive(indata, _frames: int, _time, status) -> None:
            nonlocal received, callback_error
            if control.cancel.is_set() or control.stop.is_set():
                complete.set()
                return
            if status:
                callback_error = str(status)
            remaining = frames * channels * 2 - received
            chunk = bytes(indata)[:max(0, remaining)]
            if chunk:
                chunks.append(chunk)
                received += len(chunk)
                control.duration_ms = received * 1000 // (sample_rate * channels * 2)
            if received >= frames * channels * 2:
                complete.set()

        try:
            with sd.RawInputStream(samplerate=sample_rate, channels=channels, blocksize=blocksize,
                                   dtype="int16", device=device, callback=receive):
                deadline = time.monotonic() + seconds + 2
                while not complete.wait(0.01):
                    if control.cancel.is_set() or control.stop.is_set():
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("麦克风采集超时，请检查输入设备。")
        finally:
            control.closed.set()
        return b"".join(chunks), callback_error
