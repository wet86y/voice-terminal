"""Device media framing（设备音频消息封装），独立于代理桥接协议。"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = 1  # PROTOCOL_VERSION（设备音频协议版本）。
MAX_HEADER_BYTES = 4096  # MAX_HEADER_BYTES（音频帧头最大字节数）。
MAX_PAYLOAD_BYTES = 256 * 1024  # MAX_PAYLOAD_BYTES（音频帧负载最大字节数）。
MAX_OPUS_PACKET_BYTES = 1275  # MAX_OPUS_PACKET_BYTES（单个 raw Opus 包的严格上限）。
_ALLOWED_RATES = frozenset({16_000, 24_000, 32_000, 48_000})  # _ALLOWED_RATES（设备 PCM 采样率清单）。
_PREFIX = struct.Struct("!I")  # _PREFIX（四字节网络字节序帧头长度）。


class MediaProtocolError(ValueError):
    """MediaProtocolError（设备音频协议错误）。"""


@dataclass(frozen=True)
class MediaFrame:
    """MediaFrame（传输音频帧）：数据在协议边界完成长度和字段检查。"""

    stream_id: str  # stream_id（连续音频流编号）。
    sequence: int  # sequence（本流连续帧序号）。
    start_sample: int  # start_sample（16 kHz 采样起始位置）。
    sample_count: int  # sample_count（帧内有效采样数）。
    device_time_us: int  # device_time_us（设备单调采集时刻，微秒）。
    payload: bytes  # payload（二进制 PCM 音频负载）。
    device_id: str  # device_id（设备编号）。
    core_session_id: str  # core_session_id（权威核心运行编号）。
    connection_generation: int  # connection_generation（连接代次）。
    executor_generation: int  # executor_generation（设备执行器代次）。
    interaction_generation: int  # interaction_generation（语音占用代次）。
    discontinuity_before: bool = False  # discontinuity_before（本帧前是否发生采集缺口）。
    audio_codec: str = "pcm"  # audio_codec（协商的上行负载格式，旧帧缺省兼容 PCM）。


@dataclass(frozen=True)
class ClientTtsAudioFrame:
    """ClientTtsAudioFrame（客户端在线合成上行帧）：按原语音任务固定设备及代次。"""

    device_id: str  # device_id（已认证的客户端设备编号）。
    core_session_id: str  # core_session_id（权威核心运行编号）。
    connection_generation: int  # connection_generation（设备连接代次）。
    executor_generation: int  # executor_generation（设备执行器代次）。
    interaction_generation: int  # interaction_generation（主机授予的语音占用代次）。
    stream_id: str  # stream_id（SSH 媒体通道编号）。
    task_id: str  # task_id（固定语音事务编号）。
    request_id: str  # request_id（MiniMax 文本分段请求编号）。
    sequence: int  # sequence（请求内单调递增的 PCM 帧序号）。
    start_sample: int  # start_sample（该请求 PCM 时间线位置）。
    sample_count: int  # sample_count（帧内有效采样数）。
    sample_rate: int  # sample_rate（PCM 采样率）。
    device_time_us: int  # device_time_us（客户端单调封帧时刻，微秒）。
    payload: bytes  # payload（二进制小端 int16 单声道 PCM 负载）。


_CLIENT_TTS_AUDIO_HEADER_FIELDS = frozenset({
    "protocol_version", "direction", "device_id", "core_session_id", "connection_generation",
    "executor_generation", "interaction_generation", "stream_id", "task_id", "request_id",
    "sequence", "start_sample", "sample_count", "sample_rate", "device_time_us",
})  # _CLIENT_TTS_AUDIO_HEADER_FIELDS（客户端合成帧 v1 的精确 JSON 字段集）。


def _validate_client_tts_audio_frame(frame: ClientTtsAudioFrame) -> None:
    for name, value in (
        ("device_id", frame.device_id), ("core_session_id", frame.core_session_id),
        ("stream_id", frame.stream_id), ("task_id", frame.task_id), ("request_id", frame.request_id),
    ):
        if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip():
            raise MediaProtocolError(f"客户端合成帧字段 {name}（编号）无效。")
    for name, value, allow_zero in (
        ("connection_generation", frame.connection_generation, False),
        ("executor_generation", frame.executor_generation, False),
        ("interaction_generation", frame.interaction_generation, False),
        ("sequence", frame.sequence, True), ("start_sample", frame.start_sample, True),
        ("sample_count", frame.sample_count, False), ("sample_rate", frame.sample_rate, False),
        ("device_time_us", frame.device_time_us, True),
    ):
        if type(value) is not int or value < (0 if allow_zero else 1) or value > (1 << 53) - 1:
            raise MediaProtocolError(f"客户端合成帧字段 {name}（数值）无效。")
    if frame.sample_rate not in _ALLOWED_RATES or frame.sample_count > frame.sample_rate // 50:
        raise MediaProtocolError("客户端合成帧采样率或 20 毫秒长度无效。")
    if frame.start_sample + frame.sample_count > (1 << 53) - 1:
        raise MediaProtocolError("客户端合成帧采样位置溢出。")
    if (not isinstance(frame.payload, bytes) or not frame.payload
            or len(frame.payload) != frame.sample_count * 2 or len(frame.payload) > MAX_PAYLOAD_BYTES):
        raise MediaProtocolError("客户端合成帧 PCM 负载与采样数不一致。")


def encode_client_tts_audio_frame(frame: ClientTtsAudioFrame) -> bytes:
    """encode_client_tts_audio_frame（编码上行帧）：PCM 原样写入二进制负载。"""
    if not isinstance(frame, ClientTtsAudioFrame):
        raise TypeError("客户端合成上行帧必须为 ClientTtsAudioFrame。")
    _validate_client_tts_audio_frame(frame)
    header = {
        "protocol_version": PROTOCOL_VERSION,
        "direction": "tts_audio",
        "device_id": frame.device_id,
        "core_session_id": frame.core_session_id,
        "connection_generation": frame.connection_generation,
        "executor_generation": frame.executor_generation,
        "interaction_generation": frame.interaction_generation,
        "stream_id": frame.stream_id,
        "task_id": frame.task_id,
        "request_id": frame.request_id,
        "sequence": frame.sequence,
        "start_sample": frame.start_sample,
        "sample_count": frame.sample_count,
        "sample_rate": frame.sample_rate,
        "device_time_us": frame.device_time_us,
    }
    try:
        encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MediaProtocolError("客户端合成帧头无法编码。") from exc
    if not 0 < len(encoded) <= MAX_HEADER_BYTES:
        raise MediaProtocolError("客户端合成帧头超过长度上限。")
    return _PREFIX.pack(len(encoded)) + encoded + frame.payload


def decode_client_tts_audio_frame(message: bytes) -> ClientTtsAudioFrame:
    """decode_client_tts_audio_frame（解码上行帧）：验长度后解析任务身份和 PCM。"""
    if not isinstance(message, bytes) or len(message) < _PREFIX.size:
        raise MediaProtocolError("客户端合成消息缺少长度前缀。")
    if len(message) > _PREFIX.size + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES:
        raise MediaProtocolError("客户端合成消息超过总长度上限。")
    header_size = _PREFIX.unpack_from(message)[0]
    if not 0 < header_size <= MAX_HEADER_BYTES or len(message) <= _PREFIX.size + header_size:
        raise MediaProtocolError("客户端合成帧头或 PCM 负载长度无效。")
    try:
        header = json.loads(
            message[_PREFIX.size:_PREFIX.size + header_size].decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(MediaProtocolError("帧头不允许非有限数值。")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MediaProtocolError("客户端合成帧头不是有效 JSON（结构化数据）。") from exc
    if (not isinstance(header, dict) or set(header) != _CLIENT_TTS_AUDIO_HEADER_FIELDS
            or type(header["protocol_version"]) is not int or header["protocol_version"] != PROTOCOL_VERSION
            or header["direction"] != "tts_audio"):
        raise MediaProtocolError("客户端合成帧版本、方向或字段不符合版本 1。")
    payload = message[_PREFIX.size + header_size:]
    frame = ClientTtsAudioFrame(
        device_id=header["device_id"], core_session_id=header["core_session_id"],
        connection_generation=header["connection_generation"], executor_generation=header["executor_generation"],
        interaction_generation=header["interaction_generation"], stream_id=header["stream_id"],
        task_id=header["task_id"], request_id=header["request_id"], sequence=header["sequence"],
        start_sample=header["start_sample"], sample_count=header["sample_count"], sample_rate=header["sample_rate"],
        device_time_us=header["device_time_us"], payload=payload,
    )
    _validate_client_tts_audio_frame(frame)
    return frame


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise MediaProtocolError("音频帧头包含重复字段。")
        value[key] = item
    return value


def _validate_header(header: Any) -> None:
    required = {
        "protocol_version", "stream_id", "sequence", "start_sample", "sample_count", "device_time_us",
        "device_id", "core_session_id", "connection_generation", "executor_generation", "interaction_generation",
        "discontinuity_before",
    }
    if (not isinstance(header, dict)
            or not required.issubset(header)
            or set(header) not in (required, required | {"audio_codec"})):
        raise MediaProtocolError("音频帧头字段不符合版本 1。")
    codec = header.get("audio_codec", "pcm")
    if not isinstance(codec, str) or codec not in {"pcm", "opus"}:
        raise MediaProtocolError("上行音频帧编解码模式无效。")
    if type(header["protocol_version"]) is not int or header["protocol_version"] != PROTOCOL_VERSION:
        raise MediaProtocolError("设备音频协议版本不受支持。")
    for key in ("stream_id", "device_id", "core_session_id"):
        value = header[key]
        if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip():
            raise MediaProtocolError(f"音频帧字段 {key}（编号）无效。")
    values = []
    for key in ("sequence", "start_sample", "sample_count", "device_time_us", "connection_generation",
                "executor_generation", "interaction_generation"):
        item = header[key]
        if type(item) is not int or item < 0 or item > (1 << 53) - 1:
            raise MediaProtocolError(f"音频帧字段 {key}（数值）无效。")
        values.append(item)
    if values[2] == 0 or any(value == 0 for value in values[4:]):
        raise MediaProtocolError("音频帧必须包含有效采样。")
    if type(header["discontinuity_before"]) is not bool:
        raise MediaProtocolError("音频帧缺口标识必须为布尔值。")


def encode_frame(frame: MediaFrame) -> bytes:
    """encode_frame（编码帧）：使用二进制负载，不进行文本编码。"""
    if not isinstance(frame.payload, bytes) or not 0 < len(frame.payload) <= MAX_PAYLOAD_BYTES:
        raise MediaProtocolError("音频负载长度无效。")
    header = {"protocol_version": PROTOCOL_VERSION, "stream_id": frame.stream_id,
              "sequence": frame.sequence, "start_sample": frame.start_sample,
              "sample_count": frame.sample_count, "device_time_us": frame.device_time_us,
              "device_id": frame.device_id, "core_session_id": frame.core_session_id,
              "connection_generation": frame.connection_generation, "executor_generation": frame.executor_generation,
              "interaction_generation": frame.interaction_generation,
              "discontinuity_before": frame.discontinuity_before,
              "audio_codec": frame.audio_codec}
    _validate_header(header)
    if frame.audio_codec == "pcm" and len(frame.payload) != frame.sample_count * 2:
        raise MediaProtocolError("PCM 音频负载必须与有效采样数一致。")
    if frame.audio_codec == "opus" and (frame.sample_count != 320
            or not 0 < len(frame.payload) <= MAX_OPUS_PACKET_BYTES):
        raise MediaProtocolError("Opus 上行帧必须标记 320 个 16 kHz 样本位置和一个有界编码包。")
    try:
        encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MediaProtocolError("音频帧头无法编码。") from exc
    if len(encoded) > MAX_HEADER_BYTES:
        raise MediaProtocolError("音频帧头过大。")
    return _PREFIX.pack(len(encoded)) + encoded + frame.payload


def decode_frame(message: bytes) -> MediaFrame:
    """decode_frame（解码帧）：先核对消息大小，再解析受限帧头。"""
    if not isinstance(message, bytes) or len(message) < _PREFIX.size:
        raise MediaProtocolError("音频消息缺少长度前缀。")
    if len(message) > _PREFIX.size + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES:
        raise MediaProtocolError("音频消息过大。")
    header_size = _PREFIX.unpack_from(message)[0]
    if not 0 < header_size <= MAX_HEADER_BYTES or len(message) <= _PREFIX.size + header_size:
        raise MediaProtocolError("音频帧头或负载长度无效。")
    try:
        header = json.loads(message[_PREFIX.size:_PREFIX.size + header_size].decode("utf-8"),
                            object_pairs_hook=_unique_object,
                            parse_constant=lambda _value: (_ for _ in ()).throw(MediaProtocolError("不支持非有限数值。")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MediaProtocolError("音频帧头不是有效 JSON（结构化数据）。") from exc
    _validate_header(header)
    payload = message[_PREFIX.size + header_size:]
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise MediaProtocolError("音频负载过大。")
    audio_codec = header.get("audio_codec", "pcm")
    if audio_codec == "pcm" and len(payload) != header["sample_count"] * 2:
        raise MediaProtocolError("PCM 音频负载必须与有效采样数一致。")
    if audio_codec == "opus" and (header["sample_count"] != 320
            or not 0 < len(payload) <= MAX_OPUS_PACKET_BYTES):
        raise MediaProtocolError("Opus 上行帧必须标记 320 个 16 kHz 样本位置和一个有界编码包。")
    return MediaFrame(header["stream_id"], header["sequence"], header["start_sample"], header["sample_count"],
                      header["device_time_us"], payload, header["device_id"], header["core_session_id"],
                      header["connection_generation"], header["executor_generation"], header["interaction_generation"],
                      header["discontinuity_before"], audio_codec)
