"""Inbound PCM stream（上行原始音频流）：校验租约、采样连续性和时钟。"""
from __future__ import annotations

import math
from collections.abc import Callable

from ..controller.turn_workspace import AudioFrame
from .media_protocol import MediaFrame, MediaProtocolError
from .opus_codec import OpusDecoder
from .registry import DeviceRegistry, InteractionLease


class PcmInputStream:
    """PcmInputStream（原始音频输入流）：一次缺口后必须建立新流。"""

    def __init__(self, *, stream_id: str, core_session_id: str, lease: InteractionLease, registry: DeviceRegistry,
                 map_device_time: Callable[[int], float | tuple[float, float]],
                 audio_codec: str = "pcm", encoder_pre_skip_samples: int = 0) -> None:
        if not isinstance(stream_id, str) or not stream_id or len(stream_id) > 128:
            raise ValueError("音频流编号无效。")
        self.stream_id = stream_id
        if (not isinstance(core_session_id, str) or not core_session_id or len(core_session_id) > 128
                or core_session_id != core_session_id.strip()):
            raise ValueError("核心运行编号无效。")
        self.core_session_id = core_session_id
        self.lease = lease
        self._registry = registry
        self._map_device_time = map_device_time
        if audio_codec not in {"pcm", "opus"}:
            raise ValueError("上行音频编解码模式无效。")
        if (type(encoder_pre_skip_samples) is not int or not 0 <= encoder_pre_skip_samples <= 320
                or (audio_codec == "pcm" and encoder_pre_skip_samples != 0)):
            raise ValueError("编码预延迟样本数无效。")
        self.audio_codec = audio_codec  # audio_codec（已在签名媒体握手中协商的上行编码）。
        self.encoder_pre_skip_samples = encoder_pre_skip_samples  # encoder_pre_skip_samples（Opus 编码器预延迟）。
        self._opus_decoder = OpusDecoder(16_000) if audio_codec == "opus" else None
        self._opus_pre_skip_remaining = encoder_pre_skip_samples
        self._opus_buffer = bytearray()
        self._opus_stream_start_sample: int | None = None
        self._opus_stream_start_host_time: float | None = None
        self._opus_output_samples = 0
        self._opus_uncertainty: float | None = None
        self._next_sequence = 0
        self._next_sample: int | None = None
        self._last_device_time: int | None = None
        self._last_host_time: float | None = None
        self.clock_uncertainty_seconds: float | None = None  # clock_uncertainty_seconds（最近采集帧的时钟误差）。
        self.closed = False

    def close(self) -> None:
        """close（关闭流）：之后不能继续接纳帧。"""
        self.closed = True
        if self._opus_decoder is not None:
            self._opus_decoder.close()
            self._opus_decoder = None
        self._opus_buffer.clear()

    def accept(self, frame: MediaFrame) -> AudioFrame | None:
        """accept（接纳帧）：PCM 即时返回；Opus 先剔除编码预延迟并累计有效样本。"""
        if self.closed:
            raise MediaProtocolError("音频流已经关闭。")
        try:
            if not self._registry.accepts(self.lease):
                raise MediaProtocolError("交互设备租约已失效。")
            if (any(type(value) is not int for value in (frame.sequence, frame.connection_generation,
                                                          frame.executor_generation, frame.interaction_generation))
                    or frame.device_id != self.lease.device_id or frame.core_session_id != self.core_session_id
                    or frame.connection_generation != self.lease.connection_generation
                    or frame.executor_generation != self.lease.executor_generation
                    or frame.interaction_generation != self.lease.interaction_generation):
                raise MediaProtocolError("音频帧设备、核心或交互代次不匹配。")
            if frame.stream_id != self.stream_id or frame.sequence != self._next_sequence:
                raise MediaProtocolError("音频流编号或帧序号不连续。")
            if frame.audio_codec != self.audio_codec:
                raise MediaProtocolError("音频帧编解码模式与签名媒体握手不一致。")
            if (type(frame.start_sample) is not int or frame.start_sample < 0
                    or type(frame.sample_count) is not int or not 0 < frame.sample_count <= 320
                    or type(frame.device_time_us) is not int or frame.device_time_us < 0
                    or type(frame.discontinuity_before) is not bool
                    or not isinstance(frame.payload, bytes)):
                raise MediaProtocolError("原始音频采样格式或长度无效。")
            if self.audio_codec == "pcm" and len(frame.payload) != frame.sample_count * 2:
                raise MediaProtocolError("PCM 音频负载长度与有效采样数不一致。")
            if self.audio_codec == "opus" and (frame.sample_count != 320
                    or not 0 < len(frame.payload) <= 1275):
                raise MediaProtocolError("Opus 帧必须包含一个有效的 20 ms 单声道包。")
            if frame.discontinuity_before:
                raise MediaProtocolError("设备采集帧前存在采样缺口，必须重建音频流。")
            if self._next_sample is not None and frame.start_sample != self._next_sample:
                raise MediaProtocolError("音频采样位置不连续。")
            if self._last_device_time is not None and frame.device_time_us <= self._last_device_time:
                raise MediaProtocolError("设备采集时钟不连续。")
            mapped = self._map_device_time(frame.device_time_us)
            uncertainty = None
            if isinstance(mapped, tuple):
                if len(mapped) != 2:
                    raise MediaProtocolError("设备时钟映射结果无效。")
                host_time, uncertainty = mapped
                if (isinstance(uncertainty, bool) or not isinstance(uncertainty, (float, int))
                        or not math.isfinite(uncertainty) or uncertainty < 0):
                    raise MediaProtocolError("设备时钟误差无效。")
            else:
                host_time = mapped
            if (isinstance(host_time, bool) or not isinstance(host_time, (float, int))
                    or not math.isfinite(host_time)
                    or (self._last_host_time is not None and host_time <= self._last_host_time)):
                raise MediaProtocolError("设备时钟无法映射到可信的主机时间。")
            self._next_sequence += 1
            self._next_sample = frame.start_sample + frame.sample_count
            self._last_device_time = frame.device_time_us
            self._last_host_time = float(host_time)
            self.clock_uncertainty_seconds = float(uncertainty) if uncertainty is not None else None
            if self._opus_decoder is None:
                return AudioFrame(frame.payload, frame.start_sample, float(host_time), self.clock_uncertainty_seconds)
            decoded = self._opus_decoder.decode(frame.payload)
            if self._opus_stream_start_sample is None:
                self._opus_stream_start_sample = frame.start_sample
                self._opus_stream_start_host_time = float(host_time)
            if self._opus_pre_skip_remaining:
                skipped = min(self._opus_pre_skip_remaining, len(decoded) // 2)
                decoded = decoded[skipped * 2:]
                self._opus_pre_skip_remaining -= skipped
            self._opus_buffer.extend(decoded)
            self._opus_uncertainty = float(uncertainty) if uncertainty is not None else self._opus_uncertainty
            output_bytes = 320 * 2
            if len(self._opus_buffer) < output_bytes:
                return None
            pcm = bytes(self._opus_buffer[:output_bytes])
            del self._opus_buffer[:output_bytes]
            start_sample = self._opus_stream_start_sample + self._opus_output_samples
            captured_at = self._opus_stream_start_host_time + self._opus_output_samples / 16_000
            self._opus_output_samples += 320
            return AudioFrame(pcm, start_sample, captured_at, self._opus_uncertainty)
        except Exception:
            self.close()
            raise
