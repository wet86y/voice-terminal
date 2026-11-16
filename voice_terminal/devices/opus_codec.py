"""Fixed libopus 1.6.1 stream codec（固定版本 Opus 有状态编解码器）。"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
from threading import Lock


OPUS_FRAME_MS = 20  # OPUS_FRAME_MS（实验音频帧时长，毫秒）。
MAX_OPUS_PACKET_BYTES = 1275  # MAX_OPUS_PACKET_BYTES（单个 Opus 包的严格上限）。
_OPUS_APPLICATION_VOIP = 2048
_OPUS_SET_BITRATE_REQUEST = 4002
_OPUS_SET_DTX_REQUEST = 4016
_OPUS_GET_LOOKAHEAD_REQUEST = 4027
_OPUS_OK = 0
_OPUS_RATES = frozenset({16_000, 48_000})
_DEFAULT_BITRATE = {16_000: 48_000, 48_000: 64_000}
_LIBRARY_LOCK = Lock()
_LIBRARY_CACHE: dict[str, ctypes.CDLL] = {}


class OpusCodecError(RuntimeError):
    """Opus 编码器或解码器返回错误。"""


def _library_candidates() -> tuple[Path, ...]:
    """固定查找本机产品载荷或受管开发构建，不搜索任意 PATH（命令搜索路径）。"""
    app_root = Path(__file__).resolve().parents[2]
    return (
        app_root / "opus.dll",
        app_root / ".local/build/audio/opus-shared-install/bin/opus.dll",
    )


def find_opus_library() -> Path | None:
    """返回项目固定位置的 libopus DLL（动态链接库），缺失时返回空值。"""
    return next((path for path in _library_candidates() if path.is_file()), None)


def _bind_library(path: Path) -> ctypes.CDLL:
    try:
        library = ctypes.CDLL(str(path))
    except OSError as error:
        raise OpusCodecError(f"无法加载固定 libopus 共享库：{path}") from error

    library.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    library.opus_encoder_create.restype = ctypes.c_void_p
    library.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.opus_encoder_ctl.restype = ctypes.c_int
    library.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    library.opus_encoder_destroy.restype = None
    library.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    library.opus_encode.restype = ctypes.c_int
    library.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    library.opus_decoder_create.restype = ctypes.c_void_p
    library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    library.opus_decoder_destroy.restype = None
    library.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
    library.opus_decode.restype = ctypes.c_int
    library.opus_strerror.argtypes = [ctypes.c_int]
    library.opus_strerror.restype = ctypes.c_char_p
    return library


def _load_library(path: str | Path | None = None) -> ctypes.CDLL:
    resolved = Path(path).resolve() if path is not None else find_opus_library()
    if resolved is None or not resolved.is_file():
        raise OpusCodecError("固定 libopus 1.6.1 运行库未准备；设备音频模式仍须使用 PCM（脉冲编码调制）。")
    key = str(resolved).casefold()
    with _LIBRARY_LOCK:
        library = _LIBRARY_CACHE.get(key)
        if library is None:
            library = _bind_library(resolved)
            _LIBRARY_CACHE[key] = library
        return library


def _check_result(library: ctypes.CDLL, result: int, operation: str) -> None:
    if result != _OPUS_OK:
        detail = library.opus_strerror(result)
        message = detail.decode("ascii", errors="replace") if detail else f"错误码 {result}"
        raise OpusCodecError(f"{operation}失败：{message}")


def _validate_rate(sample_rate: int) -> int:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate not in _OPUS_RATES:
        raise ValueError("Opus 编码率只接受 16 kHz 上行或 48 kHz 下行输入；32 kHz 合成音频须先重采样。")
    return sample_rate


def _pcm_input(pcm: bytes, frame_samples: int) -> ctypes.Array[ctypes.c_short]:
    if sys.byteorder != "little":
        raise OpusCodecError("当前 Opus PCM 适配仅支持 Windows 小端采样格式。")
    if not isinstance(pcm, bytes) or len(pcm) != frame_samples * 2:
        raise ValueError(f"Opus 输入必须恰为一个 20 ms 单声道 PCM 帧（{frame_samples * 2} 字节）。")
    return (ctypes.c_int16 * frame_samples).from_buffer_copy(pcm)


class OpusEncoder:
    """有状态 Opus 编码器；一个实例只属于一个连续流。"""

    def __init__(self, sample_rate: int, bitrate: int | None = None, *, library_path: str | Path | None = None) -> None:
        self.sample_rate = _validate_rate(sample_rate)
        self.frame_samples = self.sample_rate // (1000 // OPUS_FRAME_MS)
        self.bitrate = _DEFAULT_BITRATE[self.sample_rate] if bitrate is None else bitrate
        if isinstance(self.bitrate, bool) or not isinstance(self.bitrate, int) or not 6_000 <= self.bitrate <= 510_000:
            raise ValueError("Opus 码率必须位于 6–510 kbps 的标准范围内。")
        self._library = _load_library(library_path)
        self._lock = Lock()
        error = ctypes.c_int()
        self._encoder = self._library.opus_encoder_create(self.sample_rate, 1, _OPUS_APPLICATION_VOIP,
                                                          ctypes.byref(error))
        if not self._encoder:
            _check_result(self._library, error.value, "创建 Opus 编码器")
            raise OpusCodecError("libopus 未返回编码器实例。")
        try:
            _check_result(self._library, self._library.opus_encoder_ctl(
                self._encoder, _OPUS_SET_BITRATE_REQUEST, ctypes.c_int(self.bitrate)), "设置 Opus 码率")
            _check_result(self._library, self._library.opus_encoder_ctl(
                self._encoder, _OPUS_SET_DTX_REQUEST, ctypes.c_int(0)), "关闭 Opus DTX（不连续传输）")
            lookahead = ctypes.c_int()
            _check_result(self._library, self._library.opus_encoder_ctl(
                self._encoder, _OPUS_GET_LOOKAHEAD_REQUEST, ctypes.byref(lookahead)), "读取 Opus 编码预延迟")
            if lookahead.value < 0 or lookahead.value > self.sample_rate:
                raise OpusCodecError("libopus 返回的编码预延迟样本数无效。")
            self.lookahead_samples = lookahead.value  # lookahead_samples（编码器预延迟采样数）。
        except Exception:
            self.close()
            raise

    def encode(self, pcm: bytes) -> bytes:
        """编码一帧 PCM 并保留同一实例的跨帧预测状态。"""
        input_frame = _pcm_input(pcm, self.frame_samples)
        output = (ctypes.c_ubyte * MAX_OPUS_PACKET_BYTES)()
        with self._lock:
            if not self._encoder:
                raise OpusCodecError("Opus 编码器已经关闭。")
            count = self._library.opus_encode(self._encoder, input_frame, self.frame_samples,
                                              output, MAX_OPUS_PACKET_BYTES)
        if count < 0:
            _check_result(self._library, count, "编码 Opus 音频帧")
        if not 0 < count <= MAX_OPUS_PACKET_BYTES:
            raise OpusCodecError("libopus 返回的编码包长度无效。")
        return ctypes.string_at(output, count)

    def close(self) -> None:
        """释放编码器；重复调用安全。"""
        with self._lock:
            if self._encoder:
                self._library.opus_encoder_destroy(self._encoder)
                self._encoder = None

    def __enter__(self) -> OpusEncoder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class OpusDecoder:
    """有状态 Opus 解码器；一个实例只属于一个连续流。"""

    def __init__(self, sample_rate: int, *, library_path: str | Path | None = None) -> None:
        self.sample_rate = _validate_rate(sample_rate)
        self.frame_samples = self.sample_rate // (1000 // OPUS_FRAME_MS)
        self._library = _load_library(library_path)
        self._lock = Lock()
        error = ctypes.c_int()
        self._decoder = self._library.opus_decoder_create(self.sample_rate, 1, ctypes.byref(error))
        if not self._decoder:
            _check_result(self._library, error.value, "创建 Opus 解码器")
            raise OpusCodecError("libopus 未返回解码器实例。")

    def decode(self, packet: bytes) -> bytes:
        """解码一个 20 ms raw Opus packet（原始 Opus 包），不接受 Ogg 封装。"""
        if not isinstance(packet, bytes) or not 0 < len(packet) <= MAX_OPUS_PACKET_BYTES:
            raise ValueError("Opus 包长度无效。")
        encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        output = (ctypes.c_int16 * self.frame_samples)()
        with self._lock:
            if not self._decoder:
                raise OpusCodecError("Opus 解码器已经关闭。")
            count = self._library.opus_decode(self._decoder, encoded, len(packet), output,
                                              self.frame_samples, 0)
        if count < 0:
            _check_result(self._library, count, "解码 Opus 音频帧")
        if count != self.frame_samples:
            raise OpusCodecError(f"Opus 解码时长不符：期望 {self.frame_samples} 个采样，收到 {count} 个。")
        return ctypes.string_at(output, count * 2)

    def close(self) -> None:
        """释放解码器；重复调用安全。"""
        with self._lock:
            if self._decoder:
                self._library.opus_decoder_destroy(self._decoder)
                self._decoder = None

    def __enter__(self) -> OpusDecoder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
