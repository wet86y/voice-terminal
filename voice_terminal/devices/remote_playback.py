"""Remote PCM playback framing（远端 PCM 播放帧与有界下行队列）。"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
import json
import struct
import time
from typing import Any

from .media_protocol import MAX_HEADER_BYTES, MAX_PAYLOAD_BYTES, PROTOCOL_VERSION, MediaProtocolError

_PREFIX = struct.Struct("!I")  # _PREFIX（四字节网络字节序帧头长度）。
_ALLOWED_RATES = frozenset({16_000, 24_000, 32_000, 48_000})  # _ALLOWED_RATES（合成输出采样率）。
MAX_PLAYBACK_QUEUE_FRAMES = 64  # MAX_PLAYBACK_QUEUE_FRAMES（设备下行队列帧数上限，最多 1.28 秒）。
MAX_PLAYBACK_CHUNK_MS = 2_000  # MAX_PLAYBACK_CHUNK_MS（单次进入下行队列的 PCM 时长上限）。
MAX_DEVICE_PLAYBACK_BUFFER_MS = 400  # MAX_DEVICE_PLAYBACK_BUFFER_MS（设备与在途储备上限，为客户端 500 毫秒回声参考延迟上限留余量）。
PLAYBACK_BUFFER_REPORT_TIMEOUT = 3.0  # PLAYBACK_BUFFER_REPORT_TIMEOUT（未收到设备储备回执的等待上限，秒）。
PLAYBACK_OUTPUT_STALL_SECONDS = 6.0  # PLAYBACK_OUTPUT_STALL_SECONDS（设备停止消费后的等待上限）。


@dataclass(frozen=True)
class PlaybackMediaFrame:
    """PlaybackMediaFrame（下行播放帧）：播放目标和事务随每帧固定。"""

    device_id: str  # device_id（通过认证的设备身份编号）。
    delivery_device_id: str  # delivery_device_id（固定输出设备编号）。
    core_session_id: str  # core_session_id（权威核心运行编号）。
    connection_generation: int  # connection_generation（设备连接代次）。
    executor_generation: int  # executor_generation（设备执行器代次）。
    interaction_generation: int  # interaction_generation（发送帧时的语音占用代次）。
    stream_id: str  # stream_id（共享上行输入的连续媒体通道编号）。
    playback_key: str  # playback_key（固定播放事务编号）。
    sequence: int  # sequence（本连续通道内的播放帧序号）。
    start_sample: int  # start_sample（该播放事务内的首个采样位置）。
    sample_count: int  # sample_count（本帧有效采样数）。
    sample_rate: int  # sample_rate（PCM 采样率，支持 16/24/32/48 kHz）。
    host_send_time_us: int  # host_send_time_us（核心单调时钟发送时刻，微秒）。
    payload: bytes  # payload（二进制小端 int16 单声道 PCM 负载）。
    audio_codec: str = "pcm"  # audio_codec（下行负载格式；当前生产下行仍为 PCM）。


def _validate_identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip():
        raise MediaProtocolError(f"播放帧字段 {field}（编号）无效。")


def _validate_frame(frame: PlaybackMediaFrame) -> None:
    for value, field in (
        (frame.device_id, "device_id"), (frame.delivery_device_id, "delivery_device_id"),
        (frame.core_session_id, "core_session_id"), (frame.stream_id, "stream_id"),
        (frame.playback_key, "playback_key"),
    ):
        _validate_identifier(value, field)
    if frame.device_id != frame.delivery_device_id:
        raise MediaProtocolError("播放帧目标必须与已认证设备身份一致。")
    for value, field, allow_zero in (
        (frame.connection_generation, "connection_generation", False),
        (frame.executor_generation, "executor_generation", False),
        (frame.interaction_generation, "interaction_generation", False),
        (frame.sequence, "sequence", True),
        (frame.start_sample, "start_sample", True),
        (frame.sample_count, "sample_count", False),
        (frame.sample_rate, "sample_rate", False),
        (frame.host_send_time_us, "host_send_time_us", True),
    ):
        if type(value) is not int or value < (0 if allow_zero else 1) or value > (1 << 53) - 1:
            raise MediaProtocolError(f"播放帧字段 {field}（数值）无效。")
    if frame.sample_rate not in _ALLOWED_RATES or frame.sample_count > frame.sample_rate // 50:
        raise MediaProtocolError("播放帧采样率或 20 毫秒帧长度无效。")
    if frame.audio_codec == "pcm":
        if not isinstance(frame.payload, bytes) or len(frame.payload) != frame.sample_count * 2:
            raise MediaProtocolError("PCM 播放帧负载必须与 16 位单声道采样数一致。")
        if not frame.payload or len(frame.payload) > MAX_PAYLOAD_BYTES:
            raise MediaProtocolError("PCM 播放帧负载长度无效。")
    elif frame.audio_codec == "opus":
        if (frame.sample_rate != 48_000 or frame.sample_count > 960
                or not isinstance(frame.payload, bytes) or not 0 < len(frame.payload) <= 1275):
            raise MediaProtocolError("Opus 播放帧必须是 48 kHz 20 ms 包并带有效样本映射。")
    else:
        raise MediaProtocolError("播放帧下行编码模式无效。")


def encode_playback_frame(frame: PlaybackMediaFrame) -> bytes:
    """encode_playback_frame（编码下行帧）：无 Base64，头部和 PCM 负载分离。"""
    if not isinstance(frame, PlaybackMediaFrame):
        raise TypeError("下行媒体帧必须为 PlaybackMediaFrame。")
    _validate_frame(frame)
    header = {
        "protocol_version": PROTOCOL_VERSION,
        "direction": "playback",
        "device_id": frame.device_id,
        "delivery_device_id": frame.delivery_device_id,
        "core_session_id": frame.core_session_id,
        "connection_generation": frame.connection_generation,
        "executor_generation": frame.executor_generation,
        "interaction_generation": frame.interaction_generation,
        "stream_id": frame.stream_id,
        "playback_key": frame.playback_key,
        "sequence": frame.sequence,
        "start_sample": frame.start_sample,
        "sample_count": frame.sample_count,
        "sample_rate": frame.sample_rate,
        "host_send_time_us": frame.host_send_time_us,
        "audio_codec": frame.audio_codec,
    }
    encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not encoded or len(encoded) > MAX_HEADER_BYTES:
        raise MediaProtocolError("播放帧头超过长度上限。")
    return _PREFIX.pack(len(encoded)) + encoded + frame.payload


def decode_playback_frame(message: bytes) -> PlaybackMediaFrame:
    """decode_playback_frame（解码下行帧）：验长度后才解析 JSON 和负载。"""
    if not isinstance(message, bytes) or len(message) < _PREFIX.size:
        raise MediaProtocolError("播放消息缺少长度前缀。")
    if len(message) > _PREFIX.size + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES:
        raise MediaProtocolError("播放消息超过总长度上限。")
    header_size = _PREFIX.unpack_from(message)[0]
    if not 0 < header_size <= MAX_HEADER_BYTES or len(message) <= _PREFIX.size + header_size:
        raise MediaProtocolError("播放帧头或二进制负载长度无效。")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MediaProtocolError("播放帧头包含重复字段。")
            result[key] = value
        return result

    try:
        header = json.loads(
            message[_PREFIX.size:_PREFIX.size + header_size].decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(MediaProtocolError("播放帧头不允许非有限数值。")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MediaProtocolError("播放帧头不是有效 JSON（结构化数据）。") from exc
    expected = {
        "protocol_version", "direction", "device_id", "delivery_device_id", "core_session_id",
        "connection_generation", "executor_generation", "interaction_generation", "stream_id",
        "playback_key", "sequence", "start_sample", "sample_count", "sample_rate", "host_send_time_us",
    }
    if not isinstance(header, dict) or set(header) not in (expected, expected | {"audio_codec"}):
        raise MediaProtocolError("播放帧头字段不符合版本 1。")
    if type(header["protocol_version"]) is not int or header["protocol_version"] != PROTOCOL_VERSION \
            or header["direction"] != "playback":
        raise MediaProtocolError("媒体帧版本或下行方向无效。")
    payload = message[_PREFIX.size + header_size:]
    frame = PlaybackMediaFrame(
        device_id=header["device_id"], delivery_device_id=header["delivery_device_id"],
        core_session_id=header["core_session_id"], connection_generation=header["connection_generation"],
        executor_generation=header["executor_generation"], interaction_generation=header["interaction_generation"],
        stream_id=header["stream_id"], playback_key=header["playback_key"], sequence=header["sequence"],
        start_sample=header["start_sample"], sample_count=header["sample_count"], sample_rate=header["sample_rate"],
        host_send_time_us=header["host_send_time_us"], payload=payload,
        audio_codec=header.get("audio_codec", "pcm"),
    )
    _validate_frame(frame)
    return frame


class _OpusPlaybackStream:
    """_OpusPlaybackStream（单播放键下行流）：跨块重采样、编码及有效采样映射。"""

    FRAME_SAMPLES = 960  # FRAME_SAMPLES（48 kHz 20 毫秒单声道 Opus 帧采样数）。

    def __init__(self, source_rate: int, expected_pre_skip_samples: int) -> None:
        from .opus_codec import OpusEncoder

        self.source_rate = source_rate  # source_rate（该播放键固定的 TTS 输入采样率）。
        self.encoder = OpusEncoder(48_000, 64_000)
        self.pre_skip_samples = self.encoder.lookahead_samples  # pre_skip_samples（Opus 解码器起始丢弃采样数）。
        if self.pre_skip_samples != expected_pre_skip_samples:
            self.encoder.close()
            raise MediaProtocolError("服务端 Opus 编码器预延迟与签名媒体握手不匹配。")
        self.resampler = None  # resampler（跨 PCM 分块保存相位与滤波尾部的 soxr 状态）。
        if source_rate != 48_000:
            try:
                import soxr
                self.resampler = soxr.ResampleStream(
                    source_rate, 48_000, 1, dtype="int16", quality="HQ",
                )
            except Exception:
                self.encoder.close()
                raise
        self.pcm_buffer = bytearray()  # pcm_buffer（等待凑满 960 样本的重采样 PCM）。
        self.total_samples = 0  # total_samples（重采样后实际有效源样本总数）。
        self.output_cursor = 0  # output_cursor（已映射到 Opus 解码输出时间线的有效样本数）。
        self.packet_count = 0  # packet_count（该播放键已创建的有状态编码包数）。
        self.closed = False  # closed（编码流是否已完成或回收）。

    def push(self, pcm: bytes) -> None:
        if self.closed:
            raise RuntimeError("Opus 播放键编码流已经结束。")
        if self.resampler is None:
            converted = pcm
        else:
            import numpy as np
            converted = self.resampler.resample_chunk(
                np.frombuffer(pcm, dtype="<i2"),
            ).astype("<i2", copy=False).tobytes()
        if len(converted) % 2:
            raise MediaProtocolError("soxr 重采样器返回了不完整的 PCM16 样本。")
        self.total_samples += len(converted) // 2
        self.pcm_buffer.extend(converted)

    def finish_input(self) -> None:
        if self.closed:
            raise RuntimeError("Opus 播放键编码流已经结束。")
        if self.resampler is not None:
            import numpy as np
            tail = self.resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True)
            converted = tail.astype("<i2", copy=False).tobytes()
            if len(converted) % 2:
                raise MediaProtocolError("soxr 尾部重采样返回了不完整的 PCM16 样本。")
            self.total_samples += len(converted) // 2
            self.pcm_buffer.extend(converted)
            self.resampler = None

    def next_packet(self, *, finishing: bool) -> tuple[int, int, bytes] | None:
        """next_packet（编码下一包）：常规包需满帧，结束时准确补齐预延迟与尾部。"""
        if self.closed:
            raise RuntimeError("Opus 播放键编码流已经结束。")
        remaining = self.total_samples - self.output_cursor
        if remaining <= 0:
            return None
        buffered_samples = len(self.pcm_buffer) // 2
        if buffered_samples < self.FRAME_SAMPLES and not finishing:
            return None

        valid_limit = self.FRAME_SAMPLES - self.pre_skip_samples if self.packet_count == 0 else self.FRAME_SAMPLES
        valid_samples = min(valid_limit, remaining) if finishing else valid_limit
        if valid_samples <= 0:
            return None

        take_samples = min(buffered_samples, self.FRAME_SAMPLES)
        consumed = self.pcm_buffer[:take_samples * 2]
        del self.pcm_buffer[:take_samples * 2]
        if take_samples < self.FRAME_SAMPLES:
            consumed += b"\x00\x00" * (self.FRAME_SAMPLES - take_samples)
        return self.encode_block(bytes(consumed), valid_samples=valid_samples)

    def encode_block(self, pcm: bytes, *, valid_samples: int | None = None) -> tuple[int, int, bytes]:
        if self.closed or len(pcm) != self.FRAME_SAMPLES * 2:
            raise ValueError("Opus 播放编码输入必须恰为一个有效 20 毫秒 PCM 帧。")
        packet = self.encoder.encode(pcm)
        maximum_valid = self.FRAME_SAMPLES - self.pre_skip_samples if self.packet_count == 0 else self.FRAME_SAMPLES
        count = maximum_valid if valid_samples is None else min(maximum_valid, valid_samples)
        if count <= 0:
            raise MediaProtocolError("Opus 播放帧没有可映射到源音频的有效样本。")
        start_sample = self.output_cursor
        self.output_cursor += count
        self.packet_count += 1
        return start_sample, count, packet

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.resampler is not None:
            self.resampler.clear()
            self.resampler = None
        self.pcm_buffer.clear()
        self.encoder.close()


class RemotePlaybackOutbox:
    """RemotePlaybackOutbox（单媒体会话下行）：将 PCM 分割为有界 20 毫秒帧。"""

    def __init__(
        self, *, device_id: str, core_session_id: str, connection_generation: int,
        executor_generation: int, interaction_generation: int, stream_id: str,
        downlink_codec: str = "pcm", downlink_encoder_pre_skip_samples: int = 0,
        max_frames: int = MAX_PLAYBACK_QUEUE_FRAMES,
    ) -> None:
        if type(max_frames) is not int or not 1 <= max_frames <= 256:
            raise ValueError("远端播放队列上限必须为 1 至 256 帧。")
        if downlink_codec not in {"pcm", "opus"}:
            raise ValueError("远端播放编码只支持 PCM 或 Opus。")
        if (type(downlink_encoder_pre_skip_samples) is not int
                or not 0 <= downlink_encoder_pre_skip_samples <= 960
                or (downlink_codec == "pcm" and downlink_encoder_pre_skip_samples != 0)
                or (downlink_codec == "opus" and downlink_encoder_pre_skip_samples == 0)):
            raise ValueError("远端播放编码预延迟与下行编码模式不匹配。")
        self.device_id = device_id  # device_id（通过认证的固定输出设备）。
        self.core_session_id = core_session_id  # core_session_id（权威核心运行编号）。
        self.connection_generation = connection_generation  # connection_generation（连接代次）。
        self.executor_generation = executor_generation  # executor_generation（执行器代次）。
        self.interaction_generation = interaction_generation  # interaction_generation（媒体通道租约代次）。
        self.stream_id = stream_id  # stream_id（与双向媒体通道一致的编号）。
        self.downlink_codec = downlink_codec  # downlink_codec（本媒体会话固定的下行编码模式）。
        self.downlink_encoder_pre_skip_samples = downlink_encoder_pre_skip_samples  # downlink_encoder_pre_skip_samples（签名握手中的编码预延迟）。
        self._queue: asyncio.Queue[PlaybackMediaFrame | None] = asyncio.Queue(maxsize=max_frames)
        self._capacity = asyncio.Semaphore(max_frames)  # _capacity（等待设备接纳后才复用的有界发送窗口）。
        self._max_frames = max_frames  # _max_frames（未确认帧的最大数量）。
        self._pending_frames: dict[int, PlaybackMediaFrame] = {}  # _pending_frames（已发送、未收到设备接纳回执的帧）。
        self._pending_samples: dict[str, int] = {}  # _pending_samples（每个播放键排队及在途的采样数）。
        self._next_wire_sequence = 0  # _next_wire_sequence（只给实际下发的帧分配连续序号）。
        self._buffer_report_sequence = 0
        self._buffer_report_at = 0.0
        self._buffer_progress_at = 0.0
        self.paused = False  # paused（设备已确认暂停，暂不按输出停滞处理）。
        self._buffered_us = 0
        self._inflight_us = 0
        self._unreported_frames: deque[tuple[int, int]] = deque()
        self._buffer_changed = asyncio.Event()
        self._failed_keys: dict[str, str] = {}
        self._last_acknowledged = -1  # _last_acknowledged（最近连续收到的设备接纳回执序号）。
        self._samples_by_key: dict[str, int] = {}
        self._accepted_samples_by_key: dict[str, int] = {}  # _accepted_samples_by_key（设备确认接纳的可播放采样数）。
        self._rates_by_key: dict[str, int] = {}
        self._source_rates_by_key: dict[str, int] = {}  # _source_rates_by_key（同一事务固定的 TTS 输入采样率）。
        self._opus_streams_by_key: dict[str, _OpusPlaybackStream] = {}  # _opus_streams_by_key（每个事务独立的编码与重采样状态）。
        self._key_locks: dict[str, asyncio.Lock] = {}  # _key_locks（防止同一播放事务的入队和结束交错）。
        self._finished_keys: set[str] = set()  # _finished_keys（核心已结束追加音频的播放键）。
        self._progress_by_key: dict[str, dict[str, Any]] = {}  # _progress_by_key（最近的设备时钟播放快照）。
        self._closed = False

    def _key_lock(self, playback_key: str) -> asyncio.Lock:
        return self._key_locks.setdefault(playback_key, asyncio.Lock())

    async def enqueue(self, playback_key: str, sample_rate: int, pcm: bytes) -> int:
        """enqueue（入队播放数据）：固定目标并在有界窗口上背压，不静默丢帧。"""
        _validate_identifier(playback_key, "playback_key")
        if type(sample_rate) is not int or sample_rate not in _ALLOWED_RATES:
            raise ValueError("远端播放采样率必须为 16、24、32 或 48 kHz。")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("远端播放 PCM 必须为非空 int16 单声道字节。")
        samples = len(pcm) // 2
        if samples * 1_000 > sample_rate * MAX_PLAYBACK_CHUNK_MS:
            raise ValueError("单次远端播放数据不得超过 2000 毫秒。")
        async with self._key_lock(playback_key):
            if error := self.failure(playback_key):
                raise RuntimeError(error)
            if self._closed:
                raise RuntimeError("远端播放媒体通道已经关闭。")
            if playback_key in self._finished_keys:
                raise RuntimeError("已结束的远端播放键不能追加音频；必须使用新的播放事务编号。")
            previous_rate = self._source_rates_by_key.setdefault(playback_key, sample_rate)
            if previous_rate != sample_rate:
                raise ValueError("同一播放事务不能在流中途更换采样率。")
            if self.downlink_codec == "opus":
                await self._enqueue_opus_locked(playback_key, sample_rate, pcm)
            else:
                await self._enqueue_pcm_locked(playback_key, sample_rate, pcm)
        return samples * 1_000 // sample_rate

    async def _enqueue_pcm_locked(self, playback_key: str, sample_rate: int, pcm: bytes) -> None:
        self._rates_by_key.setdefault(playback_key, sample_rate)
        frame_samples = sample_rate // 50
        samples = len(pcm) // 2
        offset = 0
        while offset < samples:
            await self._capacity.acquire()
            if error := self.failure(playback_key):
                self._capacity.release()
                raise RuntimeError(error)
            if self._closed:
                self._capacity.release()
                raise RuntimeError("远端播放媒体通道已经关闭。")
            if playback_key in self._finished_keys:
                self._capacity.release()
                raise RuntimeError("已结束的远端播放键不能追加音频；必须使用新的播放事务编号。")
            count = min(frame_samples, samples - offset)
            start_sample = self._samples_by_key.get(playback_key, 0)
            frame = PlaybackMediaFrame(
                device_id=self.device_id, delivery_device_id=self.device_id,
                core_session_id=self.core_session_id,
                connection_generation=self.connection_generation,
                executor_generation=self.executor_generation,
                interaction_generation=self.interaction_generation,
                stream_id=self.stream_id, playback_key=playback_key,
                sequence=0, start_sample=start_sample, sample_count=count,
                sample_rate=sample_rate, host_send_time_us=0,
                payload=pcm[offset * 2:(offset + count) * 2],
            )
            self._publish_frame(frame)
            offset += count

    async def _enqueue_opus_locked(self, playback_key: str, sample_rate: int, pcm: bytes) -> None:
        self._rates_by_key.setdefault(playback_key, 48_000)
        stream = self._opus_streams_by_key.get(playback_key)
        if stream is None:
            stream = _OpusPlaybackStream(sample_rate, self.downlink_encoder_pre_skip_samples)
            self._opus_streams_by_key[playback_key] = stream
        stream.push(pcm)
        while len(stream.pcm_buffer) >= stream.FRAME_SAMPLES * 2:
            await self._capacity.acquire()
            if error := self.failure(playback_key):
                self._capacity.release()
                raise RuntimeError(error)
            if self._closed:
                self._capacity.release()
                raise RuntimeError("远端播放媒体通道已经关闭。")
            if playback_key in self._finished_keys:
                self._capacity.release()
                raise RuntimeError("已结束的远端播放键不能追加音频；必须使用新的播放事务编号。")
            encoded = stream.next_packet(finishing=False)
            if encoded is None:
                self._capacity.release()
                break
            start_sample, count, packet = encoded
            frame = PlaybackMediaFrame(
                device_id=self.device_id, delivery_device_id=self.device_id,
                core_session_id=self.core_session_id,
                connection_generation=self.connection_generation,
                executor_generation=self.executor_generation,
                interaction_generation=self.interaction_generation,
                stream_id=self.stream_id, playback_key=playback_key,
                sequence=0, start_sample=start_sample, sample_count=count,
                sample_rate=48_000, host_send_time_us=0,
                payload=packet, audio_codec="opus",
            )
            self._publish_frame(frame)

    def _publish_frame(self, frame: PlaybackMediaFrame) -> None:
        _validate_frame(frame)
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            self._capacity.release()
            raise RuntimeError("远端播放媒体队列容量与发送窗口状态不一致。") from exc
        self._pending_samples[frame.playback_key] = (
            self._pending_samples.get(frame.playback_key, 0) + frame.sample_count
        )
        self._samples_by_key[frame.playback_key] = frame.start_sample + frame.sample_count

    async def next_frame(self) -> PlaybackMediaFrame | None:
        """next_frame（取出待发送帧）：设备储备和在途音频共用发送额度。"""
        loop = asyncio.get_running_loop()
        while not self._closed:
            frame = await self._queue.get()
            if frame is None or self._closed:
                return None
            duration_us = (frame.sample_count * 1_000_000 + frame.sample_rate - 1) // frame.sample_rate
            wait_started = loop.time()
            while (not self._closed and not self.failure(frame.playback_key)
                   and self._buffered_us + self._inflight_us + duration_us > MAX_DEVICE_PLAYBACK_BUFFER_MS * 1_000):
                report_deadline = max(wait_started, self._buffer_report_at) + PLAYBACK_BUFFER_REPORT_TIMEOUT
                progress_deadline = max(wait_started, self._buffer_progress_at) + PLAYBACK_OUTPUT_STALL_SECONDS
                deadline = report_deadline if self.paused else min(report_deadline, progress_deadline)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.fail(frame.playback_key, "客户端播放储备没有释放，已结束本轮播放。")
                    break
                self._buffer_changed.clear()
                try:
                    await asyncio.wait_for(self._buffer_changed.wait(), remaining)
                except TimeoutError:
                    pass
            if self._closed:
                return None
            if self.failure(frame.playback_key):
                self._release_frame(frame)
                continue
            sequence = self._next_wire_sequence
            self._next_wire_sequence += 1
            frame = replace(frame, sequence=sequence, host_send_time_us=time.monotonic_ns() // 1_000)
            self._pending_frames[sequence] = frame
            self._unreported_frames.append((sequence, duration_us))
            self._inflight_us += duration_us
            return frame
        return None

    def record_buffer(self, next_sequence: int, buffered_ms: int, paused: bool = False) -> bool:
        """record_buffer（更新设备储备）：报告序号之前的帧已经反映在端点快照中。"""
        if not self._buffer_report_sequence <= next_sequence <= self._last_acknowledged + 1:
            return False
        self._buffer_report_at = asyncio.get_running_loop().time()
        if (next_sequence > self._buffer_report_sequence
                or buffered_ms * 1_000 < self._buffered_us or self.paused != paused):
            self._buffer_progress_at = self._buffer_report_at
        self._buffer_report_sequence = next_sequence
        self._buffered_us = buffered_ms * 1_000
        self.paused = paused
        while self._unreported_frames and self._unreported_frames[0][0] < next_sequence:
            _, duration = self._unreported_frames.popleft()
            self._inflight_us -= duration
        self._buffer_changed.set()
        return True

    def fail(self, playback_key: str, reason: str) -> None:
        """fail（结束单轮播放）：已失败的键不再分配发送序号。"""
        if playback_key in self._source_rates_by_key:
            self._failed_keys.setdefault(playback_key, reason)
            self._buffer_changed.set()

    def failure(self, playback_key: str) -> str | None:
        return self._failed_keys.get(playback_key)

    def _release_frame(self, frame: PlaybackMediaFrame) -> None:
        self._pending_samples[frame.playback_key] -= frame.sample_count
        self._capacity.release()

    def acknowledge(self, sequence: int, *, cancelled: bool = False, reason: str | None = None) -> bool:
        """acknowledge（设备回执）：按序释放发送额度，取消帧不计为可播放音频。"""
        if type(sequence) is not int or sequence < 0 or sequence <= self._last_acknowledged:
            return False
        if type(cancelled) is not bool:
            return False
        if sequence != self._last_acknowledged + 1:
            return False
        frame = self._pending_frames.pop(sequence, None)
        if frame is None:
            return False
        self._last_acknowledged = sequence
        if reason == "output_unavailable":
            self.fail(frame.playback_key, "客户端播放设备不可用，本轮播放已结束。")
        if not cancelled:
            self._accepted_samples_by_key[frame.playback_key] = (
                self._accepted_samples_by_key.get(frame.playback_key, 0) + frame.sample_count
            )
        self._release_frame(frame)
        return True

    def pending_frames(self) -> int:
        """pending_frames（下行待发送帧数）：提供不含音频内容的诊断值。"""
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        """closed（媒体队列关闭状态）：连接退出后旧播放目标永久失效。"""
        return self._closed

    def enqueued_ms_for_key(self, playback_key: str) -> int:
        """enqueued_ms_for_key（读取设备确认可播放时长）：不把已取消的网络帧计入输出目标。"""
        rate = self._rates_by_key.get(playback_key)
        samples = self._accepted_samples_by_key.get(playback_key, 0)
        return samples * 1_000 // rate if rate else 0

    def has_audio_for_key(self, playback_key: str) -> bool:
        """has_audio_for_key（查询是否给设备分配过 PCM 样本）：避免毫秒取整隐藏短帧。"""
        return self._samples_by_key.get(playback_key, 0) > 0

    def pending_ms_for_key(self, playback_key: str) -> int:
        """pending_ms_for_key（读取网络未接纳时长）：不把发送当成设备入队。"""
        rate = self._rates_by_key.get(playback_key)
        if not rate:
            return 0
        samples = self._pending_samples.get(playback_key, 0)
        return samples * 1_000 // rate

    def has_pending_audio(self, playback_key: str) -> bool:
        return self._pending_samples.get(playback_key, 0) > 0

    def record_progress(self, receipt: dict[str, Any]) -> bool:
        """record_progress（保存实际输出快照）：只接纳已发送帧后的单调设备时钟事实。"""
        if not isinstance(receipt, dict):
            return False
        key = receipt.get("playback_key")
        if not isinstance(key, str) or key not in self._rates_by_key:
            return False
        sequence = receipt.get("sequence")
        receipt_sequence = receipt.get("receipt_sequence")
        if (type(sequence) is not int or not 0 <= sequence <= self._last_acknowledged
                or type(receipt_sequence) is not int or receipt_sequence < 1):
            return False
        counters = [receipt.get(name) for name in (
            "enqueued_ms", "played_ms", "buffered_ms", "output_estimated_ms", "device_time_us",
        )]
        if any(type(value) is not int or value < 0 or value > (1 << 53) - 1 for value in counters):
            return False
        enqueued_ms, played_ms, buffered_ms, output_estimated_ms, _device_time_us = counters
        if played_ms > enqueued_ms or output_estimated_ms > played_ms:
            return False
        if type(receipt.get("started")) is not bool or receipt.get("precision") not in {
            "callback", "device_time", "estimated", "unknown",
        }:
            return False
        previous = self._progress_by_key.get(key)
        if previous is not None:
            if (receipt_sequence <= previous["receipt_sequence"] or sequence < previous["sequence"]
                    or enqueued_ms < previous["enqueued_ms"] or played_ms < previous["played_ms"]
                    or output_estimated_ms < previous["output_estimated_ms"]
                    or counters[4] < previous["device_time_us"]):
                return False
        self._progress_by_key[key] = dict(receipt)
        return True

    def progress(self, playback_key: str) -> dict[str, Any] | None:
        """progress（读取最近快照）：值缺失时不推断音频已开始或已播放。"""
        value = self._progress_by_key.get(playback_key)
        return dict(value) if value is not None else None

    async def finish_key(self, playback_key: str) -> bool:
        """finish_key（冻结播放键）：冲刷重采样尾部并结清 Opus 预延迟映射。"""
        if self._closed or playback_key not in self._source_rates_by_key:
            return False
        async with self._key_lock(playback_key):
            if self._closed or playback_key not in self._source_rates_by_key:
                return False
            if playback_key in self._finished_keys:
                return True
            self._finished_keys.add(playback_key)
            stream = self._opus_streams_by_key.get(playback_key)
            if stream is None:
                return True
            stream.finish_input()
            while stream.output_cursor < stream.total_samples:
                await self._capacity.acquire()
                if self.failure(playback_key):
                    self._capacity.release()
                    break
                if self._closed:
                    self._capacity.release()
                    raise RuntimeError("远端播放媒体通道已经关闭。")
                encoded = stream.next_packet(finishing=True)
                if encoded is None:
                    self._capacity.release()
                    break
                start_sample, count, packet = encoded
                frame = PlaybackMediaFrame(
                    device_id=self.device_id, delivery_device_id=self.device_id,
                    core_session_id=self.core_session_id,
                    connection_generation=self.connection_generation,
                    executor_generation=self.executor_generation,
                    interaction_generation=self.interaction_generation,
                    stream_id=self.stream_id, playback_key=playback_key,
                    sequence=0, start_sample=start_sample, sample_count=count,
                    sample_rate=48_000, host_send_time_us=0,
                    payload=packet, audio_codec="opus",
                )
                self._publish_frame(frame)
            stream.close()
            return True

    def forget_settled_key(self, playback_key: str) -> bool:
        """forget_settled_key（回收已结算播放键）：有未确认帧时拒绝清理。"""
        if not isinstance(playback_key, str) or not playback_key:
            return False
        if (playback_key in self._source_rates_by_key and playback_key not in self._finished_keys
                and not self.failure(playback_key)):
            return False
        if self.has_pending_audio(playback_key):
            return False
        self._pending_samples.pop(playback_key, None)
        self._failed_keys.pop(playback_key, None)
        self._samples_by_key.pop(playback_key, None)
        self._accepted_samples_by_key.pop(playback_key, None)
        self._rates_by_key.pop(playback_key, None)
        self._source_rates_by_key.pop(playback_key, None)
        self._progress_by_key.pop(playback_key, None)
        stream = self._opus_streams_by_key.pop(playback_key, None)
        if stream is not None:
            stream.close()
        self._finished_keys.discard(playback_key)
        self._key_locks.pop(playback_key, None)
        return True

    async def close(self) -> None:
        """close（关闭下行队列）：清除旧待发帧并唤醒唯一消费者。"""
        if self._closed:
            return
        self._closed = True
        self._pending_frames.clear()
        self._pending_samples.clear()
        self._failed_keys.clear()
        self._unreported_frames.clear()
        self._buffer_changed.set()
        self._progress_by_key.clear()
        self._samples_by_key.clear()
        self._accepted_samples_by_key.clear()
        self._rates_by_key.clear()
        self._source_rates_by_key.clear()
        for stream in self._opus_streams_by_key.values():
            stream.close()
        self._opus_streams_by_key.clear()
        self._key_locks.clear()
        self._finished_keys.clear()
        for _ in range(self._max_frames):
            self._capacity.release()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(None)
