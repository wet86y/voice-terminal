"""Device media framing（设备音频消息封装），独立于代理桥接协议。"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any


PROTOCOL_VERSION = 2  # PROTOCOL_VERSION（设备媒体协议版本；客户端 TTS 不回传 PCM）。
MAX_HEADER_BYTES = 4096  # MAX_HEADER_BYTES（音频帧头最大字节数）。
MAX_PAYLOAD_BYTES = 256 * 1024  # MAX_PAYLOAD_BYTES（音频帧负载最大字节数）。
MAX_CONTROL_BYTES = 16 * 1024  # MAX_CONTROL_BYTES（媒体控制消息最大字节数）。
MAX_OPUS_PACKET_BYTES = 1275  # MAX_OPUS_PACKET_BYTES（单个 raw Opus 包的严格上限）。
_ALLOWED_RATES = frozenset({16_000, 24_000, 32_000, 48_000})  # _ALLOWED_RATES（设备 PCM 采样率清单）。
_PREFIX = struct.Struct("!I")  # _PREFIX（四字节网络字节序帧头长度）。
_PROCESSING_STATES = frozenset({"ready", "degraded", "error", "unavailable"})  # _PROCESSING_STATES（允许的设备前处理健康状态）。
_PROCESSING_FIELDS = frozenset({
    "state", "near_end_voice", "protected_interruption_available", "reverse_generation", "error", "fields",
})  # _PROCESSING_FIELDS（设备前处理状态对象的精确字段集）。
_PROCESSING_LABELS = {
    "state": "AEC、降噪与近端 VAD 前处理健康状态",
    "near_end_voice": "当前采集帧是否检测到近端语音；null 表示不可用",
    "protected_interruption_available": "同设备反向参考是否允许受保护打断",
    "reverse_generation": "设备本地播放反向参考格式代次",
    "error": "前处理故障摘要；健康时为空",
}  # _PROCESSING_LABELS（设备前处理元数据字段中文说明）。

_TTS_BASE_FIELDS = frozenset({
    "type", "protocol_version", "device_id", "core_session_id", "connection_generation",
    "executor_generation", "interaction_generation", "stream_id",
})  # _TTS_BASE_FIELDS（媒体 v2 客户端直放共用身份字段）。
_TTS_CONTROL_FIELDS: dict[str, frozenset[str]] = {
    "media.tts.authorize": frozenset({"authorization_id", "playback_key", "provider", "ttl_ms", "credential"}),
    "media.tts.authorization_accepted": frozenset({"authorization_id", "playback_key"}),
    "media.tts.authorization_revoked": frozenset({"authorization_id", "playback_key"}),
    "media.tts.request": frozenset({"authorization_id", "playback_key", "request_id", "segment_index",
                                      "playback_start_sample", "text", "model", "voice_id", "sample_rate",
                                      "speed", "volume", "emotion"}),
    "media.tts.cancel": frozenset({"authorization_id", "playback_key", "request_id", "segment_index", "reason"}),
    "media.tts.accepted": frozenset({"authorization_id", "playback_key", "request_id", "segment_index", "accepted"}),
    "media.tts.started": frozenset({"authorization_id", "playback_key", "request_id", "segment_index"}),
    "media.tts.alignment": frozenset({"authorization_id", "playback_key", "request_id", "segment_index", "event",
                                       "sample_cursor", "generated_samples", "sentence_index", "sentence_start_sample", "alignment"}),
    "media.tts.segment_finished": frozenset({"authorization_id", "playback_key", "request_id", "segment_index",
                                              "text_chars", "start_sample", "end_sample"}),
    "media.tts.output_progress": frozenset({"authorization_id", "playback_key", "request_id", "segment_index",
                                             "progress_sequence", "generated_samples", "accepted_samples", "played_samples",
                                             "buffered_samples", "output_complete", "device_time_us"}),
    "media.tts.completed": frozenset({"authorization_id", "playback_key", "request_id", "segment_index",
                                      "total_samples", "playback_end_sample", "accepted_samples", "played_samples",
                                      "buffered_samples", "output_complete", "device_time_us"}),
    "media.tts.cancelled": frozenset({"authorization_id", "playback_key", "request_id", "segment_index",
                                      "accepted_samples", "played_samples", "buffered_samples",
                                      "output_complete", "device_time_us"}),
    "media.tts.failed": frozenset({"authorization_id", "playback_key", "request_id", "segment_index", "error"}),
}  # _TTS_CONTROL_FIELDS（媒体 v2 TTS 控制类型的精确字段集）。
_TTS_PROGRESS_FIELDS = frozenset({
    "accepted_samples", "played_samples", "buffered_samples", "output_complete", "device_time_us",
})  # _TTS_PROGRESS_FIELDS（客户端原生设备时钟输出证据字段集）。
_TTS_FIELD_LABELS = {
    "type": "设备媒体控制消息类型", "protocol_version": "设备媒体协议版本，必须为 2",
    "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
    "connection_generation": "SSH 设备连接代次", "executor_generation": "设备工作进程代次",
    "interaction_generation": "主机授予的语音占用代次", "stream_id": "SSH 连续媒体流编号",
    "authorization_id": "服务端签发的临时授权编号", "playback_key": "固定播放事务编号",
    "request_id": "单个文本分段请求编号", "segment_index": "播放事务内从零递增的文本分段序号",
    "playback_start_sample": "本段应接续的播放事务累计采样位置", "text": "待合成文本",
    "model": "MiniMax 合成模型编号", "voice_id": "MiniMax 音色编号", "provider": "固定在线供应商编号",
    "ttl_ms": "客户端收到授权后起算的有效毫秒数", "credential": "服务端临时授权的供应商密钥",
    "sample_rate": "客户端 PCM 输出采样率", "speed": "语速控制值", "volume": "音量控制值",
    "emotion": "可选语音情绪", "reason": "停止、替换、打断或撤销授权的固定控制原因",
    "accepted": "客户端是否接纳此播放分段", "event": "MiniMax 提供方事件类型",
    "sample_cursor": "播放事务累计 PCM 样本位置", "sentence_index": "句子顺序编号",
    "generated_samples": "客户端本段已经生成的 PCM 样本数",
    "sentence_start_sample": "播放事务累计句子起始采样位置", "alignment": "经边界约束的字词对齐数据",
    "text_chars": "已确认文本字符数", "start_sample": "播放事务累计本段起始采样位置",
    "end_sample": "播放事务累计本段结束采样位置", "progress_sequence": "当前分段内单调递增的输出进度序号",
    "accepted_samples": "固定播放事务内设备已接纳的累计 PCM 样本数",
    "played_samples": "固定播放事务内设备时钟确认已输出的累计 PCM 样本数",
    "buffered_samples": "该固定播放事务仍在设备缓冲的 PCM 样本数",
    "output_complete": "设备是否已确认固定播放事务输出完成",
    "device_time_us": "客户端原生设备时钟单调微秒值", "total_samples": "MiniMax 合成确认的本段 PCM 样本数",
    "playback_end_sample": "设备时钟确认完成的播放事务累计采样位置",
    "error": "不含凭据或请求正文的错误摘要",
}  # _TTS_FIELD_LABELS（媒体 v2 TTS 字段中文说明）。


class MediaProtocolError(ValueError):
    """MediaProtocolError（设备音频协议错误）。"""


@dataclass(frozen=True)
class AudioProcessingStatus:
    """AudioProcessingStatus（设备帧 AEC/NS/VAD 状态）：与已处理采样及播放参考代次对齐。"""

    state: str  # state（ready/degraded/error/unavailable，设备前处理健康状态）。
    near_end_voice: bool | None  # near_end_voice（近端语音结论；None 表示不可用）。
    protected_interruption_available: bool  # protected_interruption_available（是否允许受保护打断）。
    reverse_generation: int  # reverse_generation（同设备播放反向参考代次）。
    error: str | None = None  # error（可选故障摘要）。

    @classmethod
    def unavailable(cls) -> "AudioProcessingStatus":
        """unavailable（缺省状态）：不把缺少设备证据伪装成健康。"""
        return cls("unavailable", None, False, 0, "设备未提供前处理状态")

    @classmethod
    def from_wire(cls, value: Any) -> "AudioProcessingStatus":
        """from_wire（解析设备前处理对象）：字段及中文说明必须完整、精确。"""
        if not isinstance(value, dict) or set(value) != _PROCESSING_FIELDS:
            raise MediaProtocolError("音频帧缺少必需的 audio_processing（设备前处理状态）字段。")
        labels = value.get("fields")
        if (not isinstance(labels, dict) or set(labels) != set(_PROCESSING_LABELS)
                or any(not isinstance(label, str) or not label or len(label) > 100
                       or not any("\u4e00" <= char <= "\u9fff" for char in label)
                       for label in labels.values())):
            raise MediaProtocolError("设备前处理状态缺少逐字段中文说明。")
        state = value.get("state")
        near_voice = value.get("near_end_voice")
        protected = value.get("protected_interruption_available")
        reverse_generation = value.get("reverse_generation")
        error = value.get("error")
        if not isinstance(state, str) or state not in _PROCESSING_STATES:
            raise MediaProtocolError("设备前处理健康状态无效。")
        if near_voice is not None and type(near_voice) is not bool:
            raise MediaProtocolError("设备近端语音结论必须为布尔值或 null。")
        if type(protected) is not bool:
            raise MediaProtocolError("受保护打断状态必须为布尔值。")
        if type(reverse_generation) is not int or not 0 <= reverse_generation <= (1 << 53) - 1:
            raise MediaProtocolError("设备反向参考代次不是有效安全整数。")
        if error is not None and (not isinstance(error, str) or not error or len(error) > 300 or "\0" in error):
            raise MediaProtocolError("设备前处理故障摘要无效。")
        if state == "ready":
            if near_voice is None or error is not None:
                raise MediaProtocolError("健康的设备前处理必须提供近端语音结论且不能带故障。")
        elif near_voice is not None or protected:
            raise MediaProtocolError("不健康的设备前处理不能提供可信近端语音或打断保护资格。")
        return cls(state, near_voice, protected, reverse_generation, error)

    def to_wire(self) -> dict[str, Any]:
        """to_wire（生成严格媒体元数据）：包括字段中文说明。"""
        value = {
            "state": self.state,
            "near_end_voice": self.near_end_voice,
            "protected_interruption_available": self.protected_interruption_available,
            "reverse_generation": self.reverse_generation,
            "error": self.error,
            "fields": dict(_PROCESSING_LABELS),
        }
        self.from_wire(value)
        return value


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
    audio_processing: AudioProcessingStatus = field(default_factory=AudioProcessingStatus.unavailable)  # audio_processing（同帧设备前处理证据）。


@dataclass(frozen=True)
class ClientTtsAudioFrame:
    """ClientTtsAudioFrame（旧版客户端合成上行帧结构）：版本 2 编解码入口会拒绝。"""

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
    del frame
    raise MediaProtocolError("设备媒体协议第 2 版禁止客户端合成 PCM 回传。")


def encode_client_tts_audio_frame(frame: ClientTtsAudioFrame) -> bytes:
    """encode_client_tts_audio_frame（拒绝旧版合成帧）：版本 2 禁止客户端 TTS PCM 回传。"""
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
    """decode_client_tts_audio_frame（拒绝旧版合成帧）：版本 2 不解析也不保存合成 PCM。"""
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


def create_tts_control(kind: str, identity: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """create_tts_control（创建客户端直放控制消息）：生成版本 2 且逐字段附中文说明。"""
    if kind not in _TTS_CONTROL_FIELDS:
        raise MediaProtocolError("客户端直放控制类型不受支持。")
    if not isinstance(identity, dict) or set(identity) != _TTS_BASE_FIELDS - {"type", "protocol_version"}:
        raise MediaProtocolError("客户端直放消息身份字段不完整。")
    expected = set(_TTS_CONTROL_FIELDS[kind])
    if kind == "media.tts.accepted" and extra.get("accepted") is False and "error" in extra:
        expected.add("error")
    if kind == "media.tts.failed" and _TTS_PROGRESS_FIELDS.issubset(extra):
        expected.update(_TTS_PROGRESS_FIELDS)
    if set(extra) != expected:
        raise MediaProtocolError("客户端直放控制字段与消息类型不匹配。")
    message = {"type": kind, "protocol_version": PROTOCOL_VERSION, **identity, **extra}
    message["fields"] = {key: _TTS_FIELD_LABELS[key] for key in message if key != "fields"}
    validate_tts_control(message)
    return message


def parse_tts_control(payload: str | bytes) -> dict[str, Any]:
    """parse_tts_control（解析客户端直放控制消息）：检查 JSON 重复键、长度、身份字段和中文说明。"""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError as exc:
            raise MediaProtocolError("客户端直放控制消息不是 UTF-8 文本。") from exc
    if not isinstance(payload, str) or not payload or len(payload.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise MediaProtocolError("客户端直放控制消息为空或超过长度上限。")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(
                               MediaProtocolError("客户端直放控制消息不允许非有限数值。")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise MediaProtocolError("客户端直放控制消息不是有效 JSON。") from exc
    validate_tts_control(value)
    return value


def validate_tts_control(value: Any) -> None:
    """validate_tts_control（校验媒体 v2 客户端 TTS 控制消息）：拒绝未知字段和不完整字段说明。"""
    if not isinstance(value, dict):
        raise MediaProtocolError("客户端直放控制消息必须为 JSON 对象。")
    kind = value.get("type")
    if kind not in _TTS_CONTROL_FIELDS:
        raise MediaProtocolError("客户端直放控制消息类型不受支持。")
    expected = set(_TTS_BASE_FIELDS | _TTS_CONTROL_FIELDS[kind])
    if kind == "media.tts.accepted" and value.get("accepted") is False and "error" in value:
        expected.add("error")
    if kind == "media.tts.failed" and _TTS_PROGRESS_FIELDS.issubset(value):
        expected.update(_TTS_PROGRESS_FIELDS)
    if set(value) != expected | {"fields"} or type(value.get("protocol_version")) is not int \
            or value.get("protocol_version") != PROTOCOL_VERSION:
        raise MediaProtocolError("客户端直放控制字段不符合设备媒体协议第 2 版。")
    for name in ("device_id", "core_session_id", "stream_id"):
        _validate_identifier(value[name], name)
    for name in ("connection_generation", "executor_generation", "interaction_generation"):
        _bounded_int(value[name], name, positive=True)
    for name in ("authorization_id", "request_id"):
        if name in value:
            _validate_identifier(value[name], name)
    for name in ("model", "voice_id"):
        if name in value:
            _validate_identifier(value[name], name)
    if "playback_key" in value:
        if not isinstance(value["playback_key"], str) or not value["playback_key"] \
                or len(value["playback_key"]) > 128 or value["playback_key"] != value["playback_key"].strip() \
                or "\0" in value["playback_key"]:
            raise MediaProtocolError("客户端直放 playback_key（播放事务编号）无效。")
    for name in ("segment_index", "playback_start_sample", "ttl_ms", "text_chars", "start_sample",
                 "end_sample", "progress_sequence", "generated_samples", "accepted_samples", "played_samples",
                 "buffered_samples", "device_time_us", "total_samples", "playback_end_sample",
                 "sample_cursor", "sentence_index", "sentence_start_sample"):
        if name in value:
            _bounded_int(value[name], name, positive=(name == "ttl_ms"))
    if "ttl_ms" in value and value["ttl_ms"] > 300_000:
        raise MediaProtocolError("客户端直放授权有效时长不能超过 300 秒。")
    if "provider" in value and value["provider"] != "minimax":
        raise MediaProtocolError("客户端直放授权供应商必须为 MiniMax。")
    if "credential" in value and (not isinstance(value["credential"], str)
            or not value["credential"] or len(value["credential"].encode("utf-8")) > 4096):
        raise MediaProtocolError("服务端 MiniMax 临时凭据为空或超出长度上限。")
    if "sample_rate" in value and (type(value["sample_rate"]) is not int
            or value["sample_rate"] not in _ALLOWED_RATES):
        raise MediaProtocolError("客户端直放 PCM 采样率无效。")
    for name, lower, upper in (("speed", 0.5, 2.0), ("volume", 0.0, 10.0)):
        if name in value and (isinstance(value[name], bool) or not isinstance(value[name], (int, float))
                              or not math.isfinite(value[name]) or not lower <= value[name] <= upper):
            raise MediaProtocolError(f"客户端直放字段 {name}（控制值）无效。")
    if "text" in value and (not isinstance(value["text"], str) or not value["text"].strip()
                            or len(value["text"]) > 9999 or "\0" in value["text"]):
        raise MediaProtocolError("客户端直放文本为空、超长或含无效字符。")
    if "emotion" in value and value["emotion"] is not None and value["emotion"] not in {
        "calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
    }:
        raise MediaProtocolError("客户端直放情绪字段无效。")
    if "accepted" in value and type(value["accepted"]) is not bool:
        raise MediaProtocolError("客户端直放接纳字段必须为布尔值。")
    if "accepted" in value and value["accepted"] and "error" in value:
        raise MediaProtocolError("接纳成功回执不应包含失败摘要。")
    if "error" in value and (not isinstance(value["error"], str) or not value["error"]
                             or len(value["error"]) > 300 or "\0" in value["error"]):
        raise MediaProtocolError("客户端直放错误摘要无效。")
    if "reason" in value and (not isinstance(value["reason"], str) or not value["reason"]
                              or len(value["reason"]) > 64 or "\0" in value["reason"]):
        raise MediaProtocolError("客户端直放控制原因无效。")
    if "output_complete" in value and type(value["output_complete"]) is not bool:
        raise MediaProtocolError("客户端直放输出完成字段必须为布尔值。")
    if "played_samples" in value and value["played_samples"] > value.get("accepted_samples", -1):
        raise MediaProtocolError("客户端直放已输出样本数超过设备接纳样本数。")
    if "buffered_samples" in value and value["buffered_samples"] > max(
            0, value.get("accepted_samples", 0) - value.get("played_samples", 0)):
        raise MediaProtocolError("客户端直放设备缓冲样本数超过未输出样本数。")
    if value.get("output_complete") is True and value.get("buffered_samples") != 0:
        raise MediaProtocolError("设备输出完成回执仍包含缓冲音频。")
    if "alignment" in value:
        try:
            if len(json.dumps(value["alignment"], ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > 2048:
                raise MediaProtocolError("客户端直放字词对齐数据超过长度上限。")
        except (TypeError, ValueError) as exc:
            raise MediaProtocolError("客户端直放字词对齐数据无法编码。") from exc
    labels = value.get("fields")
    expected_labels = set(value) - {"fields"}
    if not isinstance(labels, dict) or set(labels) != expected_labels \
            or any(not isinstance(label, str) or not label or len(label) > 80
                   or not any("\u4e00" <= char <= "\u9fff" for char in label)
                   for label in labels.values()):
        raise MediaProtocolError("客户端直放控制消息缺少逐字段中文说明。")


def _validate_identifier(value: Any, name: str) -> None:
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or not value.isascii() or any(not (char.isalnum() or char in "._-") for char in value)):
        raise MediaProtocolError(f"客户端直放字段 {name}（编号）无效。")


def _bounded_int(value: Any, name: str, *, positive: bool = False) -> int:
    lower = 1 if positive else 0
    if type(value) is not int or value < lower or value > (1 << 53) - 1:
        raise MediaProtocolError(f"客户端直放字段 {name}（安全整数）无效。")
    return value


def _validate_header(header: Any) -> None:
    required = {
        "protocol_version", "stream_id", "sequence", "start_sample", "sample_count", "device_time_us",
        "device_id", "core_session_id", "connection_generation", "executor_generation", "interaction_generation",
        "discontinuity_before", "audio_processing",
    }
    if not isinstance(header, dict):
        raise MediaProtocolError("音频帧头必须为对象。")
    if "audio_processing" not in header:
        raise MediaProtocolError("音频帧头缺少 audio_processing（设备前处理状态）。")
    if (not required.issubset(header)
            or set(header) not in (required, required | {"audio_codec"})):
        raise MediaProtocolError("音频帧头字段不符合媒体协议版本 2。")
    AudioProcessingStatus.from_wire(header["audio_processing"])
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
              "audio_codec": frame.audio_codec,
              "audio_processing": frame.audio_processing.to_wire()}
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
                      header["discontinuity_before"], audio_codec,
                      AudioProcessingStatus.from_wire(header["audio_processing"]))
