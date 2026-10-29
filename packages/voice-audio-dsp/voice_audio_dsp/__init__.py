"""voice_audio_dsp（语音音频处理）：SoundTouch 2.4.0 的薄 C ABI 包装。"""
from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path

__version__ = "0.1.0"


@lru_cache(maxsize=1)
def _library():
    library = ctypes.CDLL(str(Path(__file__).resolve().with_name("soundtouch.dll")))
    handle = ctypes.c_void_p
    signatures = {
        "createInstance": ([], handle),
        "destroyInstance": ([handle], None),
        "setSampleRate": ([handle, ctypes.c_uint], ctypes.c_int),
        "setChannels": ([handle, ctypes.c_uint], ctypes.c_int),
        "setTempo": ([handle, ctypes.c_float], None),
        "putSamples_i16": ([handle, ctypes.POINTER(ctypes.c_short), ctypes.c_uint], None),
        "receiveSamples_i16": ([handle, ctypes.POINTER(ctypes.c_short), ctypes.c_uint], ctypes.c_uint),
        "numSamples": ([handle], ctypes.c_uint),
        "numUnprocessedSamples": ([handle], ctypes.c_uint),
        "flush": ([handle], ctypes.c_int),
        "getVersionString": ([], ctypes.c_char_p),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, "soundtouch_" + name)
        function.argtypes, function.restype = arguments, result
    version = library.soundtouch_getVersionString().decode("ascii")
    if version != "2.4.0":
        raise RuntimeError("SoundTouch（音频处理库）版本与锁定版本不符：" + version)
    return library


def available() -> bool:
    _library()
    return True


class TempoProcessor:
    """TempoProcessor（保音高变速器）；数量一律为每声道采样帧。"""

    def __init__(self, sample_rate: int, *, channels: int = 1, speed: float = 1):
        if not 8000 <= sample_rate <= 48000 or channels not in (1, 2) or not .5 <= speed <= 2:
            raise ValueError("保音高变速的采样率、声道数或倍率无效。")
        self._library = library = _library()
        self._handle = library.soundtouch_createInstance()
        self.channels = channels
        self._finished = False
        if not self._handle:
            raise RuntimeError("无法创建保音高变速器。")
        try:
            if not library.soundtouch_setSampleRate(self._handle, sample_rate):
                raise RuntimeError("无法设置变速器采样率。")
            if not library.soundtouch_setChannels(self._handle, channels):
                raise RuntimeError("无法设置变速器声道数。")
            library.soundtouch_setTempo(self._handle, speed)
        except BaseException:
            self.close()
            raise

    def _receive(self) -> bytes:
        result = bytearray()
        while count := self._library.soundtouch_numSamples(self._handle):
            frames = min(count, 8192)
            output = (ctypes.c_short * (frames * self.channels))()
            received = self._library.soundtouch_receiveSamples_i16(self._handle, output, frames)
            if not received:
                break
            result.extend(ctypes.string_at(output, received * self.channels * 2))
        return bytes(result)

    def push(self, pcm: bytes) -> bytes:
        if self._finished or not self._handle:
            raise RuntimeError("变速器已结束。")
        if len(pcm) % (2 * self.channels):
            raise ValueError("输入音频不包含完整采样帧。")
        frames = len(pcm) // (2 * self.channels)
        if frames:
            source = (ctypes.c_short * (frames * self.channels)).from_buffer_copy(pcm)
            self._library.soundtouch_putSamples_i16(self._handle, source, frames)
        return self._receive()

    def finish(self) -> bytes:
        if self._finished or not self._handle:
            return b""
        self._finished = True
        if not self._library.soundtouch_flush(self._handle):
            raise RuntimeError("变速器尾缓冲收尾失败。")
        return self._receive()

    @property
    def buffered_input_frames(self) -> int:
        """buffered_input_frames（仍由算法持有的源采样帧），与已产出的帧分开。"""
        return int(self._library.soundtouch_numUnprocessedSamples(self._handle)) if self._handle and not self._finished else 0

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._library.soundtouch_destroyInstance(handle)

    def __del__(self):
        self.close()
