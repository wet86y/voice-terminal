"""Remote client direct TTS（远端客户端直放语音合成）：PCM never returns to the host."""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .media_protocol import (
    MediaProtocolError,
    create_tts_control,
    validate_tts_control,
)
from .registry import InteractionLease


_ALLOWED_RATES = frozenset({16_000, 24_000, 32_000, 48_000})  # _ALLOWED_RATES（客户端合成允许的 PCM 采样率）。
_MAX_TEXT_CHARS = 9_999  # _MAX_TEXT_CHARS（单个 MiniMax 文本请求字符数上限）。
_MAX_REQUESTS = 64  # _MAX_REQUESTS（一个媒体租约内的请求记录上限）。
_MAX_AUTHORIZATIONS = 8  # _MAX_AUTHORIZATIONS（一个媒体租约内的临时播放授权上限）。
_MIN_OUTPUT_MS = 80  # _MIN_OUTPUT_MS（设备必须真实输出的最短有效音频时长）。
_CONTROL_QUEUE_SIZE = 32  # _CONTROL_QUEUE_SIZE（主机至客户端的有界控制消息数）。
_OUTPUT_PROGRESS_INTERVAL = 0.1  # _OUTPUT_PROGRESS_INTERVAL（客户端输出进度轮询秒数）。


class RemoteClientTtsError(RuntimeError):
    """RemoteClientTtsError（远端客户端直放状态错误）。"""


@dataclass
class _AuthorizationState:
    authorization_id: str  # authorization_id（服务端签发的临时授权编号）。
    playback_key: str  # playback_key（授权唯一绑定的播放事务编号）。
    accepted: asyncio.Event = field(default_factory=asyncio.Event)  # accepted（客户端已在原生内存接纳授权）。
    revoked: bool = False  # revoked（授权已撤销或媒体会话已关闭）。
    error: str | None = None  # error（不包含凭据正文的授权失败摘要）。


@dataclass
class _PlaybackState:
    playback_key: str  # playback_key（固定播放事务编号）。
    authorization_id: str  # authorization_id（此播放事务的临时授权编号）。
    sample_rate: int  # sample_rate（此播放事务固定的客户端 PCM 采样率）。
    next_segment_index: int = 0  # next_segment_index（下一个合法文本段编号）。
    next_sample: int = 0  # next_sample（下一个合法的播放事务累计采样位置）。
    active: bool = False  # active（当前播放事务是否已有合成生产者）。
    terminal: bool = False  # terminal（此事务已经取消、失败、撤销或关闭）。
    accepted_samples: int = 0  # accepted_samples（设备累计接纳的 PCM 样本数）。
    played_samples: int = 0  # played_samples（设备时钟确认输出的累计 PCM 样本数）。
    buffered_samples: int = 0  # buffered_samples（该设备事务尚未输出的 PCM 样本数）。
    output_complete: bool = False  # output_complete（设备是否确认该固定事务已播放结束）。
    device_time_us: int = 0  # device_time_us（最近一次真实设备时钟回执的单调时间）。


@dataclass
class _RequestState:
    request_id: str  # request_id（播放事务内单个文本分段编号）。
    authorization_id: str  # authorization_id（服务端签发的临时授权编号）。
    playback_key: str  # playback_key（固定播放事务编号）。
    segment_index: int  # segment_index（播放事务内从零递增的文本分段序号）。
    playback_start_sample: int  # playback_start_sample（本段应接续的事务累计采样位置）。
    text_chars: int  # text_chars（服务端发送的 Unicode 字符数量）。
    sample_rate: int  # sample_rate（本段 PCM 采样率）。
    accepted: asyncio.Event = field(default_factory=asyncio.Event)  # accepted（客户端接纳了本段播放事务）。
    started: asyncio.Event = field(default_factory=asyncio.Event)  # started（客户端合成处理器启动确认）。
    segment_finished: asyncio.Event = field(default_factory=asyncio.Event)  # segment_finished（合成端确认本段 PCM 已生成）。
    finished: asyncio.Event = field(default_factory=asyncio.Event)  # finished（真实设备输出或终态回执）。
    generation_finished_at: float | None = None  # generation_finished_at（客户端生成分段边界的主机接收时刻）。
    alignment_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=512))  # alignment_events（有界字词对齐事件）。
    generated_samples: int = 0  # generated_samples（已完成生成的本段 PCM 样本数）。
    reported_generated_samples: int = 0  # reported_generated_samples（进度回执中已确认生成的 PCM 上界）。
    end_sample: int | None = None  # end_sample（本段结束位置，使用播放事务累计样本时钟）。
    last_progress_sequence: int = 0  # last_progress_sequence（最近接纳的输出进度序号）。
    error: str | None = None  # error（不含凭据或合成正文的错误摘要）。
    cancelled: bool = False  # cancelled（客户端已确认取消请求）。


class RemoteClientTtsOutbox:
    """RemoteClientTtsOutbox（绑定单个媒体租约的直放授权与回执队列）。"""

    def __init__(
        self,
        *,
        lease: InteractionLease,
        core_session_id: str,
        stream_id: str,
        max_requests: int = _MAX_REQUESTS,
    ) -> None:
        if not isinstance(lease, InteractionLease) or lease.device_id == "local":
            raise TypeError("远端客户端直放必须绑定非本机语音租约。")
        _validate_id(core_session_id, "core_session_id")
        _validate_id(stream_id, "stream_id")
        if type(max_requests) is not int or not 1 <= max_requests <= _MAX_REQUESTS:
            raise ValueError("远端直放请求记录上限必须为 1 至 64。")
        self.lease = lease  # lease（此中继固定的设备连接与语音占用代次）。
        self.core_session_id = core_session_id  # core_session_id（权威核心运行编号）。
        self.stream_id = stream_id  # stream_id（对应 SSH 连续媒体流编号）。
        self._requests: dict[str, _RequestState] = {}  # _requests（当前租约的文本分段请求台账）。
        self._order: deque[str] = deque()  # _order（文本分段提交顺序）。
        self._authorizations: dict[str, _AuthorizationState] = {}  # _authorizations（临时授权编号台账）。
        self._playbacks: dict[str, _PlaybackState] = {}  # _playbacks（播放键唯一生产者及设备输出进度）。
        self._controls: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_CONTROL_QUEUE_SIZE)
        self._max_requests = max_requests  # _max_requests（请求台账容量）。
        self._closed = False  # _closed（连接关闭后旧租约不可复用）。

    @property
    def closed(self) -> bool:
        """closed（媒体中继是否已关闭）。"""
        return self._closed

    @property
    def authorizations(self) -> tuple[tuple[str, str], ...]:
        """authorizations（当前授权编号与播放键快照，不含任何凭据）。"""
        return tuple((item.authorization_id, item.playback_key) for item in self._authorizations.values())

    async def authorize(self, *, playback_key: str, credential: str, ttl_ms: int = 120_000) -> str:
        """authorize（通过已认证媒体通道签发内存授权）：不伪称厂商短期令牌。"""
        _validate_playback_key(playback_key)
        if not isinstance(credential, str) or not credential.strip() or len(credential.encode("utf-8")) > 4096:
            raise RemoteClientTtsError("服务端 MiniMax 凭据为空或超出长度限制。")
        if type(ttl_ms) is not int or not 1_000 <= ttl_ms <= 300_000:
            raise RemoteClientTtsError("客户端直放临时授权有效时长必须为 1 至 300 秒。")
        if self._closed:
            raise RemoteClientTtsError("远端合成媒体租约已经关闭。")
        if len(self._authorizations) >= _MAX_AUTHORIZATIONS:
            raise RemoteClientTtsError("客户端直放临时授权台账已满。")
        if any(item.playback_key == playback_key for item in self._authorizations.values()):
            raise RemoteClientTtsError("同一 playback_key（播放事务编号）不可重复签发授权。")
        authorization_id = uuid4().hex
        state = _AuthorizationState(authorization_id, playback_key)
        control = self._control("media.tts.authorize", {
            "authorization_id": authorization_id,
            "playback_key": playback_key,
            "provider": "minimax",
            "ttl_ms": ttl_ms,
            "credential": credential,
        })
        self._authorizations[authorization_id] = state
        try:
            await self._controls.put(control)
        except BaseException:
            self._authorizations.pop(authorization_id, None)
            control["credential"] = ""
            raise
        return authorization_id

    async def wait_authorization(self, authorization_id: str, *, timeout_seconds: float = 15.0) -> None:
        """wait_authorization（等待客户端内存接纳授权）：授权前不发送供应商合成请求。"""
        state = self._authorizations.get(authorization_id)
        if state is None or state.revoked:
            raise RemoteClientTtsError("原客户端直放授权不在当前媒体租约台账中。")
        try:
            await asyncio.wait_for(state.accepted.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RemoteClientTtsError("客户端没有在时限内接纳直放授权。") from exc
        if state.error:
            raise RemoteClientTtsError(state.error)

    async def revoke(self, authorization_id: str, playback_key: str) -> None:
        """revoke（撤销客户端授权）：发送撤销控制并移除服务端临时会话关联。"""
        state = self._authorizations.get(authorization_id)
        if state is None:
            return
        if state.playback_key != playback_key:
            raise RemoteClientTtsError("临时授权撤销的 playback_key（播放事务编号）不匹配。")
        if not self._closed:
            await self._controls.put(self._control("media.tts.authorization_revoked", {
                "authorization_id": authorization_id,
                "playback_key": playback_key,
            }))
        state.revoked = True
        self._authorizations.pop(authorization_id, None)
        playback = self._playbacks.get(playback_key)
        if playback is not None:
            playback.terminal = True

    async def next_control(self) -> dict[str, Any] | None:
        """next_control（读取下一条客户端控制消息）：媒体发送循环调用。"""
        return await self._controls.get()

    async def submit(
        self,
        *,
        authorization_id: str,
        playback_key: str,
        request_id: str,
        segment_index: int,
        playback_start_sample: int,
        text: str,
        model: str,
        voice_id: str,
        sample_rate: int,
        speed: float,
        volume: float,
        emotion: str | None,
    ) -> _RequestState:
        """submit（派发单个文本分段）：不包含音频或服务端音频回传通道。"""
        _validate_id(authorization_id, "authorization_id")
        _validate_playback_key(playback_key)
        _validate_id(request_id, "request_id")
        _validate_id(model, "model")
        _validate_id(voice_id, "voice_id")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS or "\0" in text:
            raise ValueError("客户端合成文本为空、超长或含有无效字符。")
        if type(segment_index) is not int or segment_index < 0 or segment_index > (1 << 53) - 1:
            raise ValueError("客户端直放分段序号无效。")
        if type(playback_start_sample) is not int or playback_start_sample < 0 or playback_start_sample > (1 << 53) - 1:
            raise ValueError("客户端直放累计采样起点无效。")
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
        auth = self._authorizations.get(authorization_id)
        if auth is None or auth.revoked or auth.playback_key != playback_key:
            raise RemoteClientTtsError("单个文本分段没有匹配的服务端直放授权。")
        if not auth.accepted.is_set():
            raise RemoteClientTtsError("客户端尚未在原生内存接纳直放授权。")
        if len(self._requests) >= self._max_requests:
            self._prune_finished()
        if len(self._requests) >= self._max_requests:
            raise RemoteClientTtsError("远端直放请求台账已满，未创建新请求。")
        if request_id in self._requests:
            raise RemoteClientTtsError("分段 request_id（请求编号）重复，未创建第二次生产。")
        playback = self._playbacks.get(playback_key)
        if playback is None:
            if segment_index != 0 or playback_start_sample != 0:
                raise RemoteClientTtsError("播放事务的首分段必须从零采样位置开始。")
            playback = _PlaybackState(playback_key, authorization_id, sample_rate)
            self._playbacks[playback_key] = playback
        if (playback.authorization_id != authorization_id or playback.sample_rate != sample_rate
                or playback.terminal or playback.active
                or playback.next_segment_index != segment_index or playback.next_sample != playback_start_sample):
            raise RemoteClientTtsError("同一 playback_key（播放事务编号）已存在生产者或分段顺序不连续。")
        playback.active = True
        state = _RequestState(
            request_id=request_id, authorization_id=authorization_id, playback_key=playback_key,
            segment_index=segment_index, playback_start_sample=playback_start_sample,
            text_chars=len(text), sample_rate=sample_rate,
        )
        control = self._control("media.tts.request", {
            "authorization_id": authorization_id,
            "playback_key": playback_key,
            "request_id": request_id,
            "segment_index": segment_index,
            "playback_start_sample": playback_start_sample,
            "text": text,
            "model": model,
            "voice_id": voice_id,
            "sample_rate": sample_rate,
            "speed": float(speed),
            "volume": float(volume),
            "emotion": emotion,
        })
        self._requests[request_id] = state
        self._order.append(request_id)
        try:
            await self._controls.put(control)
        except BaseException:
            self._requests.pop(request_id, None)
            self._order.pop()
            playback.active = False
            raise
        return state

    async def cancel(self, request_id: str, *, timeout_seconds: float = 5.0, reason: str = "stop") -> bool:
        """cancel（请求客户端停止当前分段及播放键）：等待客户端和原生播放器回执。"""
        state = self._requests.get(request_id)
        if state is None:
            raise RemoteClientTtsError("原直放分段已不在当前媒体租约台账中。")
        if state.finished.is_set():
            return state.cancelled
        if self._closed:
            raise RemoteClientTtsError("媒体连接已断开，取消执行结果未知。")
        if reason not in {"stop", "replace", "interrupt", "authorization_revoked"}:
            raise ValueError("客户端直放控制原因不受支持。")
        await self._controls.put(self._control("media.tts.cancel", {
            "authorization_id": state.authorization_id,
            "playback_key": state.playback_key,
            "request_id": state.request_id,
            "segment_index": state.segment_index,
            "reason": reason,
        }))
        try:
            await asyncio.wait_for(state.finished.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RemoteClientTtsError("客户端没有在时限内确认停止合成和原生播放。") from exc
        if state.error:
            raise RemoteClientTtsError(state.error)
        return state.cancelled

    def accept_audio(self, _frame: Any) -> None:
        """accept_audio（拒绝旧 PCM 回传帧）：协议第 2 版无客户端音频上行。"""
        raise MediaProtocolError("设备媒体协议第 2 版禁止客户端合成 PCM 回传。")

    def accept_control(self, value: dict[str, Any]) -> None:
        """accept_control（接纳客户端直放回执）：验证身份代次、播放键和设备时钟证据。"""
        if self._closed or not isinstance(value, dict):
            raise MediaProtocolError("客户端直放回执属于已关闭租约或格式无效。")
        validate_tts_control(value)
        kind = value["type"]
        if kind not in {
            "media.tts.authorization_accepted", "media.tts.accepted", "media.tts.started",
            "media.tts.alignment", "media.tts.segment_finished", "media.tts.output_progress",
            "media.tts.completed", "media.tts.cancelled", "media.tts.failed",
        }:
            raise MediaProtocolError("客户端直放回执类型不受支持。")
        self._validate_identity(value)
        if kind == "media.tts.authorization_accepted":
            auth = self._authorizations.get(value["authorization_id"])
            if auth is None or auth.revoked or auth.playback_key != value["playback_key"] or auth.accepted.is_set():
                raise MediaProtocolError("客户端授权接纳回执没有匹配的等待授权。")
            auth.accepted.set()
            return
        request_id = value.get("request_id")
        state = self._requests.get(request_id) if isinstance(request_id, str) else None
        if (state is None or state.finished.is_set() or value.get("authorization_id") != state.authorization_id
                or value.get("playback_key") != state.playback_key or value.get("segment_index") != state.segment_index):
            raise MediaProtocolError("客户端直放回执没有匹配的活动播放事务分段。")
        playback = self._playbacks.get(state.playback_key)
        if playback is None or playback.terminal or not playback.active:
            raise MediaProtocolError("客户端直放回执属于已停止或被替换的播放事务。")
        if kind == "media.tts.accepted":
            if state.accepted.is_set():
                raise MediaProtocolError("客户端分段收到重复设备接纳回执。")
            state.accepted.set()
            if value["accepted"] is False:
                state.error = _bounded_text(value["error"], "error", 300)
                state.finished.set()
                playback.active = False
                playback.terminal = True
            return
        if not state.accepted.is_set() or state.error:
            raise MediaProtocolError("客户端分段在设备接纳前返回了后续回执。")
        if kind == "media.tts.started":
            if state.started.is_set():
                raise MediaProtocolError("客户端分段收到重复合成启动回执。")
            state.started.set()
            return
        if kind == "media.tts.alignment":
            if not state.started.is_set():
                raise MediaProtocolError("客户端字词对齐事件早于合成启动回执。")
            cursor = _nonnegative_int(value["sample_cursor"], "sample_cursor")
            sentence_start = _nonnegative_int(value["sentence_start_sample"], "sentence_start_sample")
            generated = _nonnegative_int(value["generated_samples"], "generated_samples")
            if (generated < state.reported_generated_samples
                    or cursor > state.playback_start_sample + generated or sentence_start > cursor):
                raise MediaProtocolError("客户端字词对齐事件引用未来样本位置。")
            state.reported_generated_samples = generated
            state.alignment_events.append({
                "event": _bounded_text(value["event"], "event", 64),
                "audio_returned_ms": cursor * 1000 // state.sample_rate,
                "sentence_index": _nonnegative_int(value["sentence_index"], "sentence_index"),
                "sentence_start_sample": sentence_start,
                "value": value["alignment"],
            })
            return
        if kind == "media.tts.segment_finished":
            if not state.started.is_set() or state.segment_finished.is_set():
                raise MediaProtocolError("客户端文本段完成回执早于启动或重复。")
            text_chars = _nonnegative_int(value["text_chars"], "text_chars")
            start_sample = _nonnegative_int(value["start_sample"], "start_sample")
            end_sample = _nonnegative_int(value["end_sample"], "end_sample")
            if (text_chars != state.text_chars or start_sample != state.playback_start_sample
                    or end_sample <= start_sample or end_sample - start_sample < state.sample_rate * _MIN_OUTPUT_MS // 1000):
                raise MediaProtocolError("客户端文本段样本范围或最小音频时长无效。")
            state.generated_samples = end_sample - start_sample
            if state.reported_generated_samples > state.generated_samples:
                raise MediaProtocolError("客户端进度回执报告的生成样本超过最终分段边界。")
            state.end_sample = end_sample
            state.generation_finished_at = time.monotonic()
            state.segment_finished.set()
            return
        if kind == "media.tts.output_progress":
            sequence = _nonnegative_int(value["progress_sequence"], "progress_sequence")
            generated = _nonnegative_int(value["generated_samples"], "generated_samples")
            if (generated < state.reported_generated_samples
                    or state.segment_finished.is_set() and generated > state.generated_samples):
                raise MediaProtocolError("客户端输出进度的已生成样本位置无效。")
            state.reported_generated_samples = generated
            upper = state.playback_start_sample + generated
            accepted = _nonnegative_int(value["accepted_samples"], "accepted_samples")
            played = _nonnegative_int(value["played_samples"], "played_samples")
            buffered = _nonnegative_int(value["buffered_samples"], "buffered_samples")
            device_time_us = _nonnegative_int(value["device_time_us"], "device_time_us")
            if (sequence <= state.last_progress_sequence or accepted < playback.accepted_samples
                    or played < playback.played_samples or accepted > upper or played > accepted
                    or buffered > accepted - played or device_time_us < playback.device_time_us
                    or value["output_complete"] and (buffered != 0 or played != accepted)):
                raise MediaProtocolError("客户端设备输出进度违反固定键或单调时钟约束。")
            state.last_progress_sequence = sequence
            playback.accepted_samples = accepted
            playback.played_samples = played
            playback.buffered_samples = buffered
            playback.output_complete = value["output_complete"]
            playback.device_time_us = device_time_us
            return
        if kind == "media.tts.completed":
            self._complete_request(state, playback, value)
            return
        if kind == "media.tts.cancelled":
            self._apply_terminal_progress(playback, value)
            state.cancelled = True
            state.finished.set()
            playback.active = False
            playback.terminal = True
            self._prune_finished()
            return
        if kind == "media.tts.failed":
            if {"accepted_samples", "played_samples", "buffered_samples", "output_complete", "device_time_us"}.issubset(value):
                self._apply_terminal_progress(playback, value)
            state.error = _bounded_text(value["error"], "error", 300)
            state.finished.set()
            playback.active = False
            playback.terminal = True
            self._prune_finished()

    def playback_progress(self, playback_key: str) -> dict[str, int | bool]:
        """playback_progress（读取服务端收到的客户端真实设备时钟快照）。"""
        playback = self._playbacks.get(playback_key)
        if playback is None:
            return {"sample_rate": 0, "accepted_samples": 0, "played_samples": 0, "buffered_samples": 0,
                    "output_complete": False, "device_time_us": 0}
        return {"sample_rate": playback.sample_rate, "accepted_samples": playback.accepted_samples, "played_samples": playback.played_samples,
                "buffered_samples": playback.buffered_samples, "output_complete": playback.output_complete,
                "device_time_us": playback.device_time_us}

    def request(self, request_id: str) -> _RequestState | None:
        """request（按编号取得请求状态）：只返回当前中继持有的分段句柄。"""
        return self._requests.get(request_id)

    def _complete_request(self, state: _RequestState, playback: _PlaybackState, value: dict[str, Any]) -> None:
        if not state.segment_finished.is_set() or state.end_sample is None or not state.started.is_set():
            raise MediaProtocolError("客户端完成回执缺少合成完成或分段范围确认。")
        total_samples = _nonnegative_int(value["total_samples"], "total_samples")
        end_sample = _nonnegative_int(value["playback_end_sample"], "playback_end_sample")
        accepted = _nonnegative_int(value["accepted_samples"], "accepted_samples")
        played = _nonnegative_int(value["played_samples"], "played_samples")
        buffered = _nonnegative_int(value["buffered_samples"], "buffered_samples")
        device_time_us = _nonnegative_int(value["device_time_us"], "device_time_us")
        if (total_samples != state.generated_samples or end_sample != state.end_sample
                or accepted < end_sample or played < end_sample or buffered != 0
                or played > accepted or not value["output_complete"]
                or value["output_complete"] and played != accepted
                or device_time_us < playback.device_time_us):
            raise MediaProtocolError("客户端合成结束没有对应的足量实际设备输出证据。")
        playback.accepted_samples = accepted
        playback.played_samples = played
        playback.buffered_samples = buffered
        playback.output_complete = True
        playback.device_time_us = device_time_us
        playback.next_segment_index += 1
        playback.next_sample = end_sample
        playback.active = False
        state.finished.set()
        self._prune_finished()

    def _apply_terminal_progress(self, playback: _PlaybackState, value: dict[str, Any]) -> None:
        accepted = _nonnegative_int(value["accepted_samples"], "accepted_samples")
        played = _nonnegative_int(value["played_samples"], "played_samples")
        buffered = _nonnegative_int(value["buffered_samples"], "buffered_samples")
        device_time_us = _nonnegative_int(value["device_time_us"], "device_time_us")
        if (accepted < playback.accepted_samples or played < playback.played_samples
                or played > accepted or buffered > accepted - played
                or device_time_us < playback.device_time_us):
            raise MediaProtocolError("客户端停止回执的设备样本进度无效。")
        playback.accepted_samples = accepted
        playback.played_samples = played
        playback.buffered_samples = buffered
        playback.output_complete = value["output_complete"]
        playback.device_time_us = device_time_us

    def _validate_identity(self, value: dict[str, Any]) -> None:
        if (value.get("device_id") != self.lease.device_id
                or value.get("core_session_id") != self.core_session_id
                or type(value.get("connection_generation")) is not int
                or value.get("connection_generation") != self.lease.connection_generation
                or type(value.get("executor_generation")) is not int
                or value.get("executor_generation") != self.lease.executor_generation
                or type(value.get("interaction_generation")) is not int
                or value.get("interaction_generation") != self.lease.interaction_generation
                or value.get("stream_id") != self.stream_id):
            raise MediaProtocolError("客户端直放回执的身份、流或代次与媒体租约不匹配。")

    def _control(self, kind: str, extra: dict[str, Any]) -> dict[str, Any]:
        return create_tts_control(kind, {
            "device_id": self.lease.device_id,
            "core_session_id": self.core_session_id,
            "connection_generation": self.lease.connection_generation,
            "executor_generation": self.lease.executor_generation,
            "interaction_generation": self.lease.interaction_generation,
            "stream_id": self.stream_id,
        }, **extra)

    def _prune_finished(self) -> None:
        while len(self._requests) >= self._max_requests:
            removable = next((request_id for request_id in self._order
                              if (state := self._requests.get(request_id)) is not None
                              and state.finished.is_set()), None)
            if removable is None:
                return
            self._order.remove(removable)
            self._requests.pop(removable, None)

    async def close(self) -> None:
        """close（关闭中继）：撤销全部临时授权并唤醒会话；断线后客户端还会清空原生内存。"""
        if self._closed:
            return
        # 先尽力发撤销，再关闭控制流；真正断线仍由原生接收循环清除 Zeroizing 授权存储。
        for authorization in tuple(self._authorizations.values()):
            try:
                self._controls.put_nowait(self._control("media.tts.authorization_revoked", {
                    "authorization_id": authorization.authorization_id,
                    "playback_key": authorization.playback_key,
                }))
            except (asyncio.QueueFull, MediaProtocolError):
                break
            authorization.revoked = True
        self._closed = True
        for state in self._requests.values():
            if not state.finished.is_set():
                state.error = "远端媒体连接中断，客户端设备输出结果未知。"
                state.finished.set()
                playback = self._playbacks.get(state.playback_key)
                if playback is not None:
                    playback.terminal = True
        for playback in self._playbacks.values():
            playback.terminal = True
        self._authorizations.clear()
        try:
            self._controls.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._controls.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._controls.put_nowait(None)


class RemoteClientTtsSession:
    """RemoteClientTtsSession（等待设备输出回执的客户端直放语音会话）。"""

    transport = "websocket_bidi"  # transport（在线双向合成媒体传输）。
    scheduling = "direct"  # scheduling（文本直送已授权客户端）。
    effective_scheduling = "direct"  # effective_scheduling（客户端原生直放调度）。
    bidi = True  # bidi（此会话可接收多个有序文本分段）。
    reusable = False  # reusable（单次播放事务独占临时授权和设备租约）。
    direct_playback = True  # direct_playback（PCM 只进入客户端原生播放器，不创建主机 PCM 读取器）。
    first_audio_ready_at: float | None = None  # first_audio_ready_at（客户端首 PCM 生成回执到达主机的时刻）。

    def __init__(
        self,
        *,
        runtime: Any,
        lease: InteractionLease,
        outbox: RemoteClientTtsOutbox,
        playback_key: str,
        credential: str,
        model: str,
        voice_id: str,
        sample_rate: int,
        emotion: str | None,
        speed: float = 1.0,
        volume: float = 1.0,
        controls: Any = None,
        authorization_ttl_ms: int = 120_000,
    ) -> None:
        _validate_playback_key(playback_key)
        if type(sample_rate) is not int or sample_rate not in _ALLOWED_RATES:
            raise ValueError("远端客户端 TTS 采样率无效。")
        if not isinstance(credential, str) or not credential:
            raise ValueError("服务端 MiniMax 凭据缺失，拒绝退回客户端本机凭据。")
        self.outbox = outbox  # outbox（固定设备租约的服务端授权和设备输出回执通道）。
        self.runtime = runtime  # runtime（核心播放事务绑定入口）。
        self.lease = lease  # lease（该合成事务固定使用的客户端交互设备租约）。
        self.playback_key = playback_key  # playback_key（固定的播放器事务编号）。
        self._credential = credential  # _credential（服务端供应商凭据，仅保留至授权消息生成后）。
        self.model = model  # model（固定 MiniMax 模型编号）。
        self.voice_id = voice_id  # voice_id（固定 MiniMax 音色编号）。
        self.sample_rate = sample_rate  # sample_rate（客户端合成 PCM 采样率）。
        self.emotion = emotion  # emotion（固定情绪控制）。
        self.speed = speed  # speed（固定语速控制）。
        self.volume = volume  # volume（固定音量控制）。
        self.controls = controls  # controls（冻结的语音参数快照）。
        self.authorization_ttl_ms = authorization_ttl_ms  # authorization_ttl_ms（原生内存授权时长）。
        self.authorization_id: str | None = None  # authorization_id（本事务的服务端授权编号）。
        self._requests: list[_RequestState] = []  # _requests（当前播放事务的有序分段）。
        self._started = False  # _started（媒体租约和授权均已验证）。
        self._closed = False  # _closed（会话已结束）。
        self._audio_eof = False  # _audio_eof（不再提交新文本）。
        self._generated_bytes = 0  # _generated_bytes（设备回执确认生成的 PCM 字节数，仅作诊断）。
        self._alignment_cursor = 0  # _alignment_cursor（已读取对齐事件数）。

    @property
    def received_bytes(self) -> int:
        """received_bytes（设备已确认生成的 PCM 字节数）：音频正文不在服务端保留。"""
        return self._generated_bytes

    @property
    def completed_segments(self) -> int:
        """completed_segments（获得设备真实输出完成回执的文本分段数）。"""
        return sum(state.finished.is_set() and state.error is None and not state.cancelled for state in self._requests)

    @property
    def generation_finished(self) -> bool:
        """generation_finished（所有已提交分段均获客户端终态回执）。"""
        return bool(self._requests) and all(state.finished.is_set() for state in self._requests)

    @property
    def alignment_events(self) -> tuple[dict[str, Any], ...]:
        """alignment_events（读取本地缓存的有界客户端字词对齐事件）。"""
        return tuple(item for state in self._requests for item in state.alignment_events)

    def playback_progress(self) -> dict[str, int | bool]:
        """playback_progress（读取服务端收到的客户端真实设备时钟快照）。"""
        return self.outbox.playback_progress(self.playback_key)

    async def start(self) -> None:
        """start（固定远端播放目标并取得客户端原生内存授权接纳回执）。"""
        if self._closed:
            raise RemoteClientTtsError("远端客户端直放会话已经关闭。")
        if self.outbox.closed or self.lease != self.outbox.lease:
            raise RemoteClientTtsError("客户端媒体租约已经关闭，不能开始直放。")
        bind = getattr(self.runtime, "bind_remote_playback_target", None)
        if not callable(bind) or not await bind(self.playback_key, self.lease):
            raise RemoteClientTtsError("无法为客户端直放固定对应的远端播放目标。")
        self.authorization_id = await self.outbox.authorize(
            playback_key=self.playback_key, credential=self._credential, ttl_ms=self.authorization_ttl_ms,
        )
        self._credential = ""
        await self.outbox.wait_authorization(self.authorization_id)
        self._started = True

    async def send_text(self, text: str) -> None:
        """send_text（提交一个文本分段）：设备确认接纳和合成启动后才返回。"""
        if not self._started or self._closed or self._audio_eof or self.authorization_id is None:
            raise RemoteClientTtsError("客户端直放会话尚未授权或已经结束。")
        if not isinstance(text, str) or not text.strip():
            raise RemoteClientTtsError("客户端拒绝空文本分段。")
        if self._requests and not self._requests[-1].finished.is_set():
            await self._requests[-1].finished.wait()
        if self._requests:
            previous = self._requests[-1]
            if previous.error:
                raise RemoteClientTtsError(previous.error)
            if previous.cancelled:
                raise RemoteClientTtsError("上一播放分段已由客户端确认取消。")
        if len(self._requests) >= _MAX_REQUESTS:
            raise RemoteClientTtsError("客户端直放单事务分段数达到上限。")
        progress = self.playback_progress()
        playback_start_sample = int(progress["accepted_samples"])
        segment_index = len(self._requests)
        state = await self.outbox.submit(
            authorization_id=self.authorization_id,
            playback_key=self.playback_key,
            request_id=uuid4().hex,
            segment_index=segment_index,
            playback_start_sample=playback_start_sample,
            text=text,
            model=self.model,
            voice_id=self.voice_id,
            sample_rate=self.sample_rate,
            speed=self.speed,
            volume=self.volume,
            emotion=self.emotion,
        )
        self._requests.append(state)
        waits = {
            asyncio.create_task(state.started.wait()),
            asyncio.create_task(state.accepted.wait()),
            asyncio.create_task(state.finished.wait()),
        }
        deadline = time.monotonic() + 15.0
        try:
            while not (state.accepted.is_set() and state.started.is_set()) and not state.finished.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                completed, waits = await asyncio.wait(
                    waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
                )
                if not completed:
                    break
        finally:
            for task in waits:
                task.cancel()
            if waits:
                await asyncio.gather(*waits, return_exceptions=True)
        if not (state.accepted.is_set() and state.started.is_set()) and not state.finished.is_set():
            state.error = "客户端分段接纳或启动回执超时，播放结果未知。"
            state.finished.set()
            raise RemoteClientTtsError(state.error)
        if state.error:
            raise RemoteClientTtsError(state.error)
        if state.cancelled:
            raise RemoteClientTtsError("客户端在合成启动前取消了分段。")
        if not state.accepted.is_set() or not state.started.is_set():
            raise RemoteClientTtsError("客户端未确认接纳并启动此播放分段。")

    async def wait_for_segment(self, previous_count: int) -> dict[str, int]:
        """wait_for_segment（等待该段生成边界和设备输出完成）：返回真实客户端采样证据。"""
        if type(previous_count) is not int or previous_count < 0 or previous_count >= len(self._requests):
            raise ValueError("客户端文本段回执索引无效。")
        state = self._requests[previous_count]
        while not state.finished.is_set():
            if state.error:
                raise RemoteClientTtsError(state.error)
            if state.cancelled:
                raise RemoteClientTtsError("客户端确认取消了当前播放事务分段。")
            await asyncio.sleep(_OUTPUT_PROGRESS_INTERVAL)
        if state.error:
            raise RemoteClientTtsError(state.error)
        if state.cancelled:
            raise RemoteClientTtsError("客户端确认取消了当前播放事务分段。")
        if state.end_sample is None:
            raise RemoteClientTtsError("客户端没有提供有效的文本分段输出边界。")
        if self.first_audio_ready_at is None:
            self.first_audio_ready_at = state.generation_finished_at or time.monotonic()
        self._generated_bytes += state.generated_samples * 2
        return {
            "index": previous_count,
            "text_chars": state.text_chars,
            "end_received_bytes": state.end_sample * 2,
            "start_sample": state.playback_start_sample,
            "end_sample": state.end_sample,
        }

    async def audio_chunks(self):
        """audio_chunks（拒绝音频读取）：直放会话不能把 PCM 暴露给服务端流水线。"""
        raise RemoteClientTtsError("客户端直放会话禁止读取或回传 PCM 音频。")
        yield b""  # Keep this a typed async generator for legacy callers; no frame can be yielded.

    async def flush(self) -> None:
        """flush（关闭文本入口并等待全部设备输出证据）：生成完成不等于设备输出完成。"""
        if self._audio_eof:
            return
        for state in self._requests:
            await state.finished.wait()
            if state.error:
                raise RemoteClientTtsError(state.error)
            if state.cancelled:
                raise RemoteClientTtsError("客户端确认取消了尚未结算的播放文本段。")
            if state.end_sample is None:
                raise RemoteClientTtsError("客户端完成回执缺少设备输出边界。")
        self._audio_eof = True

    async def finish(self) -> None:
        """finish（结束 TTS 输入）：等待所有分段的设备时钟完成回执。"""
        await self.flush()

    async def wait_until_output_complete(self, *, timeout_seconds: float = 120.0) -> dict[str, int | bool]:
        """wait_until_output_complete（等待同一播放键实际输出完成）：不接受合成完成替代设备回执。"""
        if not self._requests:
            raise RemoteClientTtsError("客户端直放没有任何已接纳的非空文本分段。")
        try:
            await asyncio.wait_for(self.flush(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RemoteClientTtsError("客户端没有在时限内确认播放事务的设备输出完成。") from exc
        progress = self.playback_progress()
        if (not progress["output_complete"] or progress["buffered_samples"] != 0
                or progress["played_samples"] < progress["accepted_samples"]
                or progress["played_samples"] < self.sample_rate * _MIN_OUTPUT_MS // 1000):
            raise RemoteClientTtsError("客户端设备实际输出不足，拒绝按播放完成结算。")
        return progress

    async def cancel(self, *, timeout_seconds: float = 5.0, reason: str = "stop") -> bool:
        """cancel（请求停止播放事务）：必须等待客户端原生播放器停止回执。"""
        if not self._requests:
            return False
        active = next((state for state in reversed(self._requests) if not state.finished.is_set()), None)
        if active is None:
            return False
        if self.outbox.closed:
            raise RemoteClientTtsError("媒体连接已断开，设备停止结果未知。")
        return await self.outbox.cancel(active.request_id, timeout_seconds=timeout_seconds, reason=reason)

    async def close(self) -> None:
        """close（释放一次播放事务）：停止未结算请求并撤销临时授权。"""
        if self._closed:
            return
        active = next((state for state in reversed(self._requests) if not state.finished.is_set()), None)
        if active is not None and not self.outbox.closed:
            try:
                await self.outbox.cancel(active.request_id, reason="authorization_revoked")
            except Exception:
                # The lease close path still clears the client authorization; this key is never re-targeted.
                pass
        self._closed = True
        self._audio_eof = True
        if self.authorization_id is not None and not self.outbox.closed:
            await self.outbox.revoke(self.authorization_id, self.playback_key)
        self._credential = ""
        unbind = getattr(self.runtime, "unbind_empty_remote_playback_target", None)
        if self._started and callable(unbind):
            # Unbind only if no client output was accepted; emitted audio remains bound to this key.
            progress = self.playback_progress()
            if not progress["accepted_samples"]:
                await unbind(self.playback_key, self.lease)


def _validate_id(value: Any, field_name: str) -> None:
    """_validate_id（验证纯 ASCII 协议编号）。"""
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or not value.isascii() or any(not (char.isalnum() or char in "._-") for char in value)):
        raise ValueError(f"{field_name}（编号）必须为 1 至 128 个 ASCII 字符。")


def _validate_playback_key(value: Any) -> None:
    """_validate_playback_key（验证项目播放事务编号）：保留现有语音会话键字符。"""
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or "\0" in value):
        raise ValueError("playback_key（播放事务编号）必须为 1 至 128 个非空字符。")


def _nonnegative_int(value: Any, field_name: str) -> int:
    """_nonnegative_int（验证跨语言安全整数）。"""
    if type(value) is not int or value < 0 or value > (1 << 53) - 1:
        raise MediaProtocolError(f"客户端直放字段 {field_name}（计数）无效。")
    return value


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    """_bounded_text（验证有界诊断文本）。"""
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise MediaProtocolError(f"客户端直放字段 {field_name}（诊断文本）无效。")
    return value
