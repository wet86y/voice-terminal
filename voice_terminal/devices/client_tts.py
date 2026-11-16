"""Remote client TTS relay（远端客户端语音合成中继）。"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .media_protocol import ClientTtsAudioFrame, MediaProtocolError
from .registry import InteractionLease


_ALLOWED_RATES = frozenset({16_000, 24_000, 32_000, 48_000})  # _ALLOWED_RATES（客户端合成允许的 PCM 采样率）。
_MAX_TEXT_CHARS = 9_999  # _MAX_TEXT_CHARS（单个 MiniMax 文本请求字符数上限）。
_MAX_REQUESTS = 64  # _MAX_REQUESTS（一个媒体租约内的请求记录上限）。
_MAX_AUDIO_BYTES = 64 * 1024 * 1024  # _MAX_AUDIO_BYTES（单个请求的 PCM 字节上限）。
_CONTROL_QUEUE_SIZE = 16  # _CONTROL_QUEUE_SIZE（主机至客户端的有界控制消息数）。
_AUDIO_QUEUE_FRAMES = 64  # _AUDIO_QUEUE_FRAMES（请求内待消费的 20 毫秒 PCM 帧数）。


class RemoteClientTtsError(RuntimeError):
    """RemoteClientTtsError（远端客户端合成状态错误）。"""


@dataclass
class _RequestState:
    request_id: str  # request_id（文本分段请求编号）。
    task_id: str  # task_id（固定语音播放事务编号）。
    text_chars: int  # text_chars（本次请求文本的字符数）。
    sample_rate: int  # sample_rate（该请求固定 PCM 采样率）。
    started: asyncio.Event = field(default_factory=asyncio.Event)  # started（客户端及提供方已确认启动）。
    segment_finished: asyncio.Event = field(default_factory=asyncio.Event)  # segment_finished（客户端确认当前文本段结束）。
    finished: asyncio.Event = field(default_factory=asyncio.Event)  # finished（请求获得终态回执）。
    audio: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_AUDIO_QUEUE_FRAMES)
    )  # audio（有界 PCM 帧队列）。
    next_sequence: int = 0  # next_sequence（下一个有效帧序号）。
    next_sample: int = 0  # next_sample（下一个有效 PCM 起始采样）。
    total_bytes: int = 0  # total_bytes（已接收 PCM 总字节数）。
    segment_receipt: dict[str, int] | None = None  # segment_receipt（客户端确认的文本段完成回执）。
    alignment_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=512))  # alignment_events（有界对齐事件缓存）。
    error: str | None = None  # error（不含凭据的远端错误摘要）。
    cancelled: bool = False  # cancelled（客户端已确认取消）。


class RemoteClientTtsOutbox:
    """RemoteClientTtsOutbox（按交互租约绑定的远端合成中继队列）。"""

    def __init__(
        self,
        *,
        lease: InteractionLease,
        core_session_id: str,
        stream_id: str,
        max_requests: int = _MAX_REQUESTS,
    ) -> None:
        if not isinstance(lease, InteractionLease) or lease.device_id == "local":
            raise TypeError("远端合成中继必须绑定非本机语音租约。")
        _validate_id(core_session_id, "core_session_id")
        _validate_id(stream_id, "stream_id")
        if type(max_requests) is not int or not 1 <= max_requests <= _MAX_REQUESTS:
            raise ValueError("远端合成请求记录上限必须为 1 至 64。")
        self.lease = lease  # lease（此中继固定的设备连接与语音占用代次）。
        self.core_session_id = core_session_id  # core_session_id（权威核心运行编号）。
        self.stream_id = stream_id  # stream_id（对应的 SSH 实时媒体流编号）。
        self._requests: dict[str, _RequestState] = {}  # _requests（本租约内尚未回收的请求台账）。
        self._order: deque[str] = deque()  # _order（TTS 分段提交顺序）。
        self._controls: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_CONTROL_QUEUE_SIZE)
        self._max_requests = max_requests  # _max_requests（请求台账容量）。
        self._closed = False  # _closed（连接关闭后旧租约不可复用）。

    @property
    def closed(self) -> bool:
        """closed（媒体中继是否已关闭）。"""
        return self._closed

    async def submit(
        self,
        *,
        task_id: str,
        text: str,
        model: str,
        voice_id: str,
        sample_rate: int,
        speed: float,
        volume: float,
        emotion: str | None,
    ) -> _RequestState:
        """submit（派发合成文本）：只发送业务参数，不接触客户端凭据。"""
        _validate_id(task_id, "task_id")
        _validate_id(model, "model")
        _validate_id(voice_id, "voice_id")
        if (not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS
                or "\0" in text):
            raise ValueError("客户端合成文本为空、超长或含有无效字符。")
        if type(sample_rate) is not int or sample_rate not in _ALLOWED_RATES:
            raise ValueError("客户端合成采样率必须为 16、24、32 或 48 kHz。")
        if (isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed)
                or not 0.5 <= speed <= 2.0 or isinstance(volume, bool)
                or not isinstance(volume, (int, float)) or not math.isfinite(volume)
                or not 0.0 <= volume <= 10.0):
            raise ValueError("客户端合成语速或音量超出支持范围。")
        if emotion is not None and emotion not in {
            "calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
        }:
            raise ValueError("客户端合成情绪不受支持。")
        if self._closed:
            raise RemoteClientTtsError("远端合成媒体租约已经关闭。")
        if len(self._requests) >= self._max_requests:
            self._prune_finished()
        if len(self._requests) >= self._max_requests:
            raise RemoteClientTtsError("远端合成请求台账已满，未创建新请求。")
        request_id = uuid4().hex
        state = _RequestState(
            request_id=request_id, task_id=task_id, text_chars=len(text), sample_rate=sample_rate,
        )
        control = {
            "type": "media.tts.request", "protocol_version": 1,
            "device_id": self.lease.device_id, "core_session_id": self.core_session_id,
            "connection_generation": self.lease.connection_generation,
            "executor_generation": self.lease.executor_generation,
            "interaction_generation": self.lease.interaction_generation,
            "stream_id": self.stream_id, "task_id": task_id, "request_id": request_id,
            "text": text, "model": model, "voice_id": voice_id,
            "sample_rate": sample_rate, "speed": float(speed), "volume": float(volume),
            "emotion": emotion,
            "fields": {
                "type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
                "connection_generation": "远端设备连接代次",
                "executor_generation": "远端设备执行器代次",
                "interaction_generation": "核心授予的语音占用代次",
                "stream_id": "连续音频流编号", "task_id": "固定语音播放事务编号",
                "request_id": "本次 MiniMax 文本分段请求编号", "text": "待合成文本",
                "model": "MiniMax 合成模型编号", "voice_id": "MiniMax 音色编号",
                "sample_rate": "客户端 PCM 输出采样率", "speed": "语速控制值",
                "volume": "音量控制值", "emotion": "可选语音情绪",
            },
        }
        self._requests[request_id] = state
        self._order.append(request_id)
        try:
            await self._controls.put(control)
        except BaseException:
            self._requests.pop(request_id, None)
            try:
                self._order.remove(request_id)
            except ValueError:
                pass
            raise
        return state

    async def next_control(self) -> dict[str, Any] | None:
        """next_control（等待下一条客户端命令）：媒体发送循环调用。"""
        return await self._controls.get()

    async def cancel(self, request_id: str, *, timeout_seconds: float = 2.0) -> bool:
        """cancel（请求客户端取消）：只有设备回执能确认取消结果。"""
        state = self._requests.get(request_id)
        if state is None:
            raise RemoteClientTtsError("原合成请求已不在当前媒体租约台账中。")
        if state.finished.is_set():
            return state.cancelled
        if self._closed:
            raise RemoteClientTtsError("媒体连接已断开，取消执行结果未知。")
        await self._controls.put({
            "type": "media.tts.cancel", "protocol_version": 1,
            "device_id": self.lease.device_id, "core_session_id": self.core_session_id,
            "connection_generation": self.lease.connection_generation,
            "executor_generation": self.lease.executor_generation,
            "interaction_generation": self.lease.interaction_generation,
            "stream_id": self.stream_id, "task_id": state.task_id, "request_id": request_id,
            "fields": {
                "type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                "device_id": "固定目标设备编号", "core_session_id": "权威核心运行编号",
                "connection_generation": "远端设备连接代次",
                "executor_generation": "远端设备执行器代次",
                "interaction_generation": "核心授予的语音占用代次",
                "stream_id": "连续音频流编号", "task_id": "固定语音播放事务编号",
                "request_id": "需取消的客户端合成请求编号",
            },
        })
        try:
            await asyncio.wait_for(state.finished.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RemoteClientTtsError("客户端没有在时限内确认取消，合成结果未知。") from exc
        if state.error:
            raise RemoteClientTtsError(state.error)
        return state.cancelled

    async def accept_audio(self, frame: ClientTtsAudioFrame) -> None:
        """accept_audio（接纳客户端 PCM）：逐帧检查租约、请求、序号与长度。"""
        if not isinstance(frame, ClientTtsAudioFrame):
            raise TypeError("远端合成帧必须先经过设备媒体协议解析。")
        if self._closed:
            raise MediaProtocolError("客户端合成帧属于已关闭媒体租约。")
        if (frame.device_id != self.lease.device_id or frame.core_session_id != self.core_session_id
                or frame.connection_generation != self.lease.connection_generation
                or frame.executor_generation != self.lease.executor_generation
                or frame.interaction_generation != self.lease.interaction_generation
                or frame.stream_id != self.stream_id):
            raise MediaProtocolError("客户端合成帧的设备、核心、流或代次与媒体租约不匹配。")
        state = self._requests.get(frame.request_id)
        if state is None or state.task_id != frame.task_id or state.finished.is_set():
            raise MediaProtocolError("客户端合成帧没有匹配的活动请求。")
        if not state.started.is_set():
            raise MediaProtocolError("客户端合成帧早于提供方启动回执。")
        if (frame.sequence != state.next_sequence or frame.start_sample != state.next_sample
                or frame.sample_rate != state.sample_rate):
            raise MediaProtocolError("客户端合成帧序号、采样位置或采样率不连续。")
        next_total = state.total_bytes + len(frame.payload)
        if next_total > _MAX_AUDIO_BYTES:
            raise MediaProtocolError("客户端单次合成 PCM 超过 64 MiB 限制。")
        await state.audio.put(frame.payload)
        state.next_sequence += 1
        state.next_sample += frame.sample_count
        state.total_bytes = next_total

    def accept_control(self, value: dict[str, Any]) -> None:
        """accept_control（接纳客户端合成回执）：拒绝代次错误与未知任务。"""
        if self._closed or not isinstance(value, dict):
            raise MediaProtocolError("客户端合成回执属于已关闭租约或格式无效。")
        kind = value.get("type")
        if kind not in {
            "media.tts.started", "media.tts.segment_finished", "media.tts.completed",
            "media.tts.cancelled", "media.tts.failed", "media.tts.provider_event",
        }:
            raise MediaProtocolError("客户端合成回执类型不受支持。")
        common = {
            "type", "protocol_version", "device_id", "core_session_id", "connection_generation",
            "executor_generation", "interaction_generation", "stream_id", "task_id", "request_id",
        }
        extra = {
            "media.tts.started": set(),
            "media.tts.segment_finished": {"text_chars", "end_sample"},
            "media.tts.completed": {"total_samples"},
            "media.tts.cancelled": set(),
            "media.tts.failed": {"error"},
            "media.tts.provider_event": {"event", "sample_cursor", "sentence_index",
                                          "sentence_start_sample", "alignment"},
        }[kind]
        if (set(value) != common | extra | {"fields"} or type(value.get("protocol_version")) is not int
                or value.get("protocol_version") != 1):
            raise MediaProtocolError("客户端合成回执字段不符合版本 1。")
        field_labels = value.get("fields")
        if (not isinstance(field_labels, dict) or set(field_labels) != common | extra
                or any(not isinstance(label, str) or not label or len(label) > 80
                       for label in field_labels.values())):
            raise MediaProtocolError("客户端合成回执缺少字段中文说明或说明格式无效。")
        if (value.get("device_id") != self.lease.device_id
                or value.get("core_session_id") != self.core_session_id
                or type(value.get("connection_generation")) is not int
                or value.get("connection_generation") != self.lease.connection_generation
                or type(value.get("executor_generation")) is not int
                or value.get("executor_generation") != self.lease.executor_generation
                or type(value.get("interaction_generation")) is not int
                or value.get("interaction_generation") != self.lease.interaction_generation
                or value.get("stream_id") != self.stream_id):
            raise MediaProtocolError("客户端合成回执的身份、流或代次与媒体租约不匹配。")
        request_id = value.get("request_id")
        state = self._requests.get(request_id) if isinstance(request_id, str) else None
        if state is None or value.get("task_id") != state.task_id or state.finished.is_set():
            raise MediaProtocolError("客户端合成回执没有匹配的活动请求。")
        if kind == "media.tts.started":
            if state.started.is_set():
                raise MediaProtocolError("客户端合成请求收到重复启动回执。")
            state.started.set()
            return
        if kind == "media.tts.provider_event":
            cursor = _nonnegative_int(value["sample_cursor"], "sample_cursor")
            if cursor > state.next_sample:
                raise MediaProtocolError("客户端对齐事件引用了尚未收到的 PCM。")
            state.alignment_events.append({
                "event": _bounded_text(value["event"], "event", 64),
                "audio_returned_ms": cursor * 1000 // state.sample_rate,
                "sentence_index": _nonnegative_int(value["sentence_index"], "sentence_index"),
                "sentence_start_sample": _nonnegative_int(value["sentence_start_sample"], "sentence_start_sample"),
                "value": value["alignment"],
            })
            return
        if kind == "media.tts.segment_finished":
            text_chars = _nonnegative_int(value["text_chars"], "text_chars")
            end_sample = _nonnegative_int(value["end_sample"], "end_sample")
            if text_chars != state.text_chars or end_sample != state.next_sample or state.segment_receipt is not None:
                raise MediaProtocolError("客户端文本段完成回执与本请求 PCM 不一致。")
            state.segment_receipt = {"text_chars": text_chars, "end_sample": end_sample}
            state.segment_finished.set()
            return
        if kind == "media.tts.completed":
            total_samples = _nonnegative_int(value["total_samples"], "total_samples")
            if state.segment_receipt is None or total_samples != state.next_sample or not state.started.is_set():
                raise MediaProtocolError("客户端合成完成回执缺少文本段确认或 PCM 样本不一致。")
        elif kind == "media.tts.cancelled":
            state.cancelled = True
        elif kind == "media.tts.failed":
            if not state.started.is_set():
                state.started.set()
            state.error = _bounded_text(value["error"], "error", 300)
        state.finished.set()
        try:
            state.audio.put_nowait(b"")
        except asyncio.QueueFull:
            # A queued sentinel is unnecessary; consumers observe ``finished``
            # after draining the already ordered PCM frames.
            pass
        self._prune_finished()

    def request(self, request_id: str) -> _RequestState | None:
        """request（按编号取得请求状态）：只返回当前中继持有的句柄。"""
        return self._requests.get(request_id)

    def _prune_finished(self) -> None:
        while len(self._requests) >= self._max_requests:
            removable = next((request_id for request_id in self._order
                              if (state := self._requests.get(request_id)) is not None
                              and state.finished.is_set() and state.audio.empty()), None)
            if removable is None:
                return
            self._order.remove(removable)
            self._requests.pop(removable, None)

    async def close(self) -> None:
        """close（关闭中继）：唤醒请求等待者并使旧设备目标失效。"""
        if self._closed:
            return
        self._closed = True
        for state in self._requests.values():
            if not state.finished.is_set():
                state.error = "远端媒体连接中断，合成结果未知。"
                state.finished.set()
                try:
                    state.audio.put_nowait(b"")
                except asyncio.QueueFull:
                    pass
        try:
            self._controls.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._controls.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._controls.put_nowait(None)


class RemoteClientTtsSession:
    """RemoteClientTtsSession（兼容播放管线的客户端 TTS 会话）。"""

    transport = "websocket_bidi"  # transport（底层在线双向合成传输类型）。
    scheduling = "direct"  # scheduling（使用在线首音直通调度）。
    effective_scheduling = "direct"  # effective_scheduling（实际播报调度策略）。
    bidi = True  # bidi（该会话允许向当前客户端合成设备继续提交分段）。
    reusable = False  # reusable（每个语音播放事务独占设备租约）。
    first_audio_ready_at: float | None = None  # first_audio_ready_at（第一帧 PCM 到达核心的单调时刻）。

    def __init__(
        self,
        *,
        runtime: Any,
        lease: InteractionLease,
        outbox: RemoteClientTtsOutbox,
        playback_key: str,
        model: str,
        voice_id: str,
        sample_rate: int,
        emotion: str | None,
        speed: float = 1.0,
        volume: float = 1.0,
        controls: Any = None,
    ) -> None:
        _validate_playback_key(playback_key)
        if type(sample_rate) is not int or sample_rate not in _ALLOWED_RATES:
            raise ValueError("远端 TTS 会话采样率无效。")
        self.outbox = outbox  # outbox（固定远端设备的合成请求中继）。
        self.runtime = runtime  # runtime（权威核心会话，用于固定输出目标并验证租约）。
        self.lease = lease  # lease（该合成事务固定使用的交互设备租约）。
        self.playback_key = playback_key  # playback_key（固定的播放器事务编号）。
        self.task_id = uuid4().hex  # task_id（不含播放键分隔符的客户端合成任务编号）。
        self.model = model  # model（固定 MiniMax 模型编号）。
        self.voice_id = voice_id  # voice_id（固定 MiniMax 音色编号）。
        self.sample_rate = sample_rate  # sample_rate（客户端合成 PCM 采样率）。
        self.emotion = emotion  # emotion（固定情绪控制）。
        self.speed = speed  # speed（固定语速控制）。
        self.volume = volume  # volume（固定音量控制）。
        self.controls = controls  # controls（受控合成会话冻结的语音参数快照）。
        self._requests: list[_RequestState] = []  # _requests（当前播报的有序分段请求）。
        self._audio_requests: asyncio.Queue[_RequestState | None] = asyncio.Queue(
            maxsize=_MAX_REQUESTS + 1,
        )  # _audio_requests（保序的有界 PCM 消费请求队列）。
        self._request_lock = asyncio.Lock()  # _request_lock（避免客户端合成请求越过文本顺序）。
        self._audio_eof = asyncio.Event()  # _audio_eof（flush 后不再提交新请求）。
        self._audio_started = asyncio.Event()  # _audio_started（音频迭代器已启动）。
        self._closed = False  # _closed（会话已关闭）。
        self._started = False  # _started（媒体租约已验证且绑定播放器事务）。
        self._completed_segments = 0  # _completed_segments（客户端确认完成的文本段数）。
        self._segment_event = asyncio.Event()  # _segment_event（文本段回执唤醒器）。
        self._received_bytes = 0  # _received_bytes（累计接收 PCM 字节数）。
        self._reader_claimed = False  # _reader_claimed（单消费者音频读取器）。

    @property
    def received_bytes(self) -> int:
        """received_bytes（已由本媒体会话接纳的 PCM 字节数）。"""
        return self._received_bytes

    @property
    def completed_segments(self) -> int:
        """completed_segments（客户端已完成的文本分段数）。"""
        return sum(state.segment_receipt is not None for state in self._requests)

    @property
    def generation_finished(self) -> bool:
        """generation_finished（所有已提交合成请求均已确认结束）。"""
        return bool(self._requests) and all(state.finished.is_set() for state in self._requests)

    @property
    def alignment_events(self) -> tuple[dict[str, Any], ...]:
        """alignment_events（读取有界客户端字词对齐事件快照）。"""
        return tuple(item for state in self._requests for item in state.alignment_events)

    async def start(self) -> None:
        """start（确认媒体租约仍在线）：具体播放器目标由构造时固定。"""
        if self._closed:
            raise RemoteClientTtsError("远端客户端合成会话已经关闭。")
        if self.outbox.closed or self.lease != self.outbox.lease:
            raise RemoteClientTtsError("客户端媒体租约已经关闭，不能开始合成。")
        bind = getattr(self.runtime, "bind_remote_playback_target", None)
        if not callable(bind) or not await bind(self.playback_key, self.lease):
            raise RemoteClientTtsError("无法为客户端合成固定对应的远端播放目标。")
        self._started = True

    async def send_text(self, text: str) -> None:
        """send_text（提交一个 Bidi 文本段）：等待前段结束并确认本段启动。"""
        if not self._started or self._closed or self._audio_eof.is_set():
            raise RemoteClientTtsError("远端客户端合成会话尚未启动或已经结束。")
        async with self._request_lock:
            if self._requests and not self._requests[-1].finished.is_set():
                await self._requests[-1].finished.wait()
                previous = self._requests[-1]
                if previous.error:
                    raise RemoteClientTtsError(previous.error)
                if previous.cancelled:
                    raise RemoteClientTtsError("上一文本段已由客户端确认取消。")
            state = await self.outbox.submit(
                task_id=self.task_id, text=text, model=self.model, voice_id=self.voice_id,
                sample_rate=self.sample_rate, speed=self.speed, volume=self.volume, emotion=self.emotion,
            )
            self._requests.append(state)
            await self._audio_requests.put(state)
            started_wait = asyncio.create_task(state.started.wait())
            finished_wait = asyncio.create_task(state.finished.wait())
            wait_tasks = (started_wait, finished_wait)
            try:
                done, _pending = await asyncio.wait(
                    wait_tasks, timeout=15.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                remaining = [task for task in wait_tasks if not task.done()]
                for task in remaining:
                    task.cancel()
                if remaining:
                    await asyncio.gather(*remaining, return_exceptions=True)
            if not done:
                state.error = "客户端启动回执超时，合成结果未知。"
                state.finished.set()
                await self.outbox.close()
                raise RemoteClientTtsError("客户端没有在 15 秒内确认 MiniMax 合成启动。")
            if state.error:
                raise RemoteClientTtsError(state.error)
            if state.cancelled:
                raise RemoteClientTtsError("客户端在合成启动前取消了请求。")
            if not state.started.is_set():
                raise RemoteClientTtsError("客户端在提供方启动前结束了合成请求。")

    async def wait_for_segment(self, previous_count: int) -> dict[str, int]:
        """wait_for_segment（等待客户端文本段完成回执）。"""
        if type(previous_count) is not int or previous_count < 0:
            raise ValueError("文本段回执索引必须为非负整数。")
        while self._completed_segments <= previous_count:
            if self._closed:
                raise RemoteClientTtsError("远端客户端合成会话已关闭。")
            if previous_count < len(self._requests):
                state = self._requests[previous_count]
                if state.segment_finished.is_set():
                    break
                if state.finished.is_set() and state.error:
                    raise RemoteClientTtsError(state.error)
                await state.segment_finished.wait()
                break
            self._segment_event.clear()
            if self._completed_segments > previous_count:
                break
            await self._segment_event.wait()
        return {"index": previous_count, "text_chars": self._requests[previous_count].text_chars,
                "end_received_bytes": self._requests[previous_count].next_sample * 2}

    async def _next_pcm(self):
        while True:
            state = await self._audio_requests.get()
            if state is None:
                return
            while True:
                if not state.audio.empty():
                    pcm = await state.audio.get()
                    if pcm:
                        self._received_bytes += len(pcm)
                        if self.first_audio_ready_at is None:
                            self.first_audio_ready_at = time.monotonic()
                        yield pcm
                    continue
                if state.finished.is_set():
                    if state.error:
                        raise RemoteClientTtsError(state.error)
                    break
                audio_task = asyncio.create_task(state.audio.get())
                done_task = asyncio.create_task(state.finished.wait())
                done, pending = await asyncio.wait(
                    (audio_task, done_task), return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if audio_task in done:
                    pcm = audio_task.result()
                    if pcm:
                        self._received_bytes += len(pcm)
                        if self.first_audio_ready_at is None:
                            self.first_audio_ready_at = time.monotonic()
                        yield pcm
                if done_task in done and state.finished.is_set() and state.audio.empty():
                    if state.error:
                        raise RemoteClientTtsError(state.error)
                    break

    async def audio_chunks(self):
        """audio_chunks（按提交顺序交付远端 PCM）：单一读取器维持文本和样本顺序。"""
        if self._reader_claimed:
            raise RemoteClientTtsError("远端客户端合成 PCM 只允许一个读取器。")
        self._reader_claimed = True
        self._audio_started.set()
        async for pcm in self._next_pcm():
            yield pcm

    async def flush(self) -> None:
        """flush（关闭文本入口并等待已提交任务结束）：不会丢弃在途 PCM。"""
        if self._audio_eof.is_set():
            return
        for state in self._requests:
            await state.finished.wait()
            if state.error:
                raise RemoteClientTtsError(state.error)
            if state.cancelled:
                raise RemoteClientTtsError("客户端确认取消了尚未结算的播放文本段。")
        self._audio_eof.set()
        await self._audio_requests.put(None)

    async def finish(self) -> None:
        """finish（结束 TTS 输入）：兼容普通流式 TTS 生命周期。"""
        await self.flush()

    async def cancel(self, *, timeout_seconds: float = 2.0) -> bool:
        """cancel（取消合成）：必须等待客户端提供方确认。"""
        if not self._requests:
            return False
        if self.outbox.closed:
            raise RemoteClientTtsError("媒体连接已断开，取消执行结果未知。")
        active = next((state for state in reversed(self._requests) if not state.finished.is_set()), None)
        if active is None:
            return False
        return await self.outbox.cancel(active.request_id, timeout_seconds=timeout_seconds)

    async def close(self) -> None:
        """close（关闭 TTS 会话）：只回收没有产生设备 PCM 的空播放目标。"""
        active = next((state for state in reversed(self._requests) if not state.finished.is_set()), None)
        if active is not None:
            await self.outbox.cancel(active.request_id)
        self._closed = True
        self._audio_eof.set()
        self._segment_event.set()
        try:
            self._audio_requests.put_nowait(None)
        except asyncio.QueueFull:
            # The bounded queue already holds the submitted work; close leaves
            # that fixed transaction for its consumer to drain before EOF.
            pass
        unbind = getattr(self.runtime, "unbind_empty_remote_playback_target", None)
        if self._started and callable(unbind):
            await unbind(self.playback_key, self.lease)


def _validate_id(value: Any, field_name: str) -> None:
    """_validate_id（验证纯 ASCII 协议编号）。"""
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or not value.isascii()
            or any(not (char.isalnum() or char in "._-") for char in value)):
        raise ValueError(f"{field_name}（编号）必须为 1 至 128 个 ASCII 字符。")


def _validate_playback_key(value: Any) -> None:
    """_validate_playback_key（验证项目播放键）：保留轮次和代次使用的冒号。"""
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or "\0" in value):
        raise ValueError("playback_key（播放事务编号）必须为 1 至 128 个非空字符。")


def _nonnegative_int(value: Any, field_name: str) -> int:
    """_nonnegative_int（验证无符号协议计数）。"""
    if type(value) is not int or value < 0 or value > (1 << 53) - 1:
        raise MediaProtocolError(f"客户端合成字段 {field_name}（计数）无效。")
    return value


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    """_bounded_text（验证有界诊断文本）。"""
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise MediaProtocolError(f"客户端合成字段 {field_name}（诊断文本）无效。")
    return value
