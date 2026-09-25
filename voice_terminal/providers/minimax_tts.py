from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import tempfile
from typing import Any

from ..tts.scheduling import TtsSegmentReceipt

logger = logging.getLogger(__name__)


def _retryable_status_code(status: Any) -> bool:
    if status is None:
        return False
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code in {408, 425, 429} or 500 <= code < 600


def _is_emotion_rejection(message: str) -> bool:
    normalized = message.casefold()
    mentions_emotion = "emotion" in normalized or "voice_setting" in normalized
    rejected = any(marker in normalized for marker in (
        "unsupported", "not supported", "unknown", "invalid", "not allowed",
        "unrecognized", "illegal", "不支持", "无效", "未知", "不合法", "不允许",
    ))
    return mentions_emotion and rejected


class MiniMaxTtsError(RuntimeError):
    pass


class MiniMaxTtsConnectionError(MiniMaxTtsError):
    """连接或 task_start 握手失败，可携带安全重试标记。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _retryable_connection_failure(exc: Exception) -> bool:
    """Retry only transient transport failures before text is submitted."""

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is not None:
        return _retryable_status_code(status)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError)):
        return True
    name = type(exc).__name__
    return name.startswith("ConnectionClosed") or name == "InvalidHandshake"


class MiniMaxEmotionUnsupported(MiniMaxTtsError):
    """The Bidi endpoint rejected the optional voice-setting extension."""

    emotion_unsupported = True


class MiniMaxTtsSession:
    def __init__(self, client: "MiniMaxTtsClient", *, emotion: str | None = None) -> None:
        self.client = client
        self.emotion = emotion
        self.bidi = getattr(client, "transport", "websocket") == "websocket_bidi"
        self._socket: Any = None
        self._receiver: asyncio.Task[None] | None = None
        # One event-loop owns the file and its independent read/write offsets.
        # Receiving never waits for playback; long answers spill to disk.
        self._audio = None
        self._read_offset = 0
        self._cache_peak_bytes = 0
        self._audio_available = asyncio.Event()
        # A Bidi connection stays alive across generations.  ``_generation_done``
        # marks the end of the current task (flush/cancel), while the receive
        # task itself continues to accept the next task_continue frame.
        self._generation_done = asyncio.Event()
        self._generation_active = False
        self._closed = False
        self._connection_dead = False
        self._server_finished = False
        self._completed_segments = 0
        self._segment_available = asyncio.Event()
        self._finished = asyncio.Event()
        self._started = False
        self._error: Exception | None = None
        self._finish_requested = False
        self._send_lock = asyncio.Lock()
        self._audio_chunks_received = 0
        self._audio_bytes_received = 0
        self._last_event: str | None = None
        self._cancel_confirmed = asyncio.Event()
        self._cancel_requested = False
        self._flush_confirmed = asyncio.Event()
        self._sentence_available = asyncio.Event()
        self._sentence_count = 0
        # MiniMax subtitle timestamps are relative to each server sentence.
        # Keep the sentence timebase and the PCM origin captured at
        # ``sentence_start`` so alignment consumers can place words on the
        # task-global playback clock.
        self._sentence_index = 0
        self._sentence_audio_base_ms = 0
        self._alignment_events: deque[dict[str, Any]] = deque(maxlen=512)
        self._cache_max_bytes = int(getattr(client, "audio_cache_max_bytes", 64 * 1024 * 1024))
        self.connection_id: int | None = None
        self._retry_attempts: dict[str, int] = {}
        self._retry_deadlines: dict[str, float] = {}
        self._sent_texts: deque[str] = deque(maxlen=256)
        self._pending_segment_chars: deque[int] = deque()
        self._segment_receipts: list[TtsSegmentReceipt] = []
        self._backpressure_idle = asyncio.Event()
        self._backpressure_idle.set()
        self._retry_cancel = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._prewarmed = False
        self._prewarm_inflight = False
        self._prewarm_ready = asyncio.Event()
        self._isolated_transaction = False

    @property
    def reusable(self) -> bool:
        """Whether the wire connection can be retained for another turn."""

        return (
            self.bidi
            and not self._closed
            and not self._connection_dead
            and self._socket is not None
            and self._error is None
            and (self._receiver is None or not self._receiver.done())
        )

    @property
    def connection_ready(self) -> bool:
        """Whether this task_start connection can accept real text now."""

        return (
            not self._closed
            and not self._connection_dead
            and self._socket is not None
            and self._error is None
            and self._started
            and (self._receiver is None or not self._receiver.done())
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def generation_finished(self) -> bool:
        return self._generation_done.is_set()

    @property
    def generation_active(self) -> bool:
        """Whether the retained Bidi connection currently has live text."""

        return self._generation_active

    @property
    def received_bytes(self) -> int:
        return self._audio_bytes_received

    @property
    def server_finished(self) -> bool:
        return self._server_finished

    @property
    def receive_finished(self) -> bool:
        return self._finished.is_set()

    @property
    def completed_segments(self) -> int:
        return self._completed_segments

    @property
    def completed_sentences(self) -> int:
        return self._sentence_count

    @property
    def alignment_events(self) -> tuple[dict[str, Any], ...]:
        """Normalized copies of subtitle/timing payloads received from TTS."""

        return tuple(self._alignment_events)

    def _received_audio_ms(self) -> int:
        bytes_per_second = max(1, int(self.client.sample_rate) * 2)
        return int(round(self._audio_bytes_received * 1000 / bytes_per_second))

    async def wait_for_segment(self, previous_count: int) -> TtsSegmentReceipt:
        """Wait for this text's is_final, never infer it from another PCM chunk."""
        while self.completed_segments <= previous_count:
            if self._error is not None:
                raise self._error
            if self.receive_finished or self._closed:
                raise MiniMaxTtsError("MiniMax TTS 未确认当前文本分段完成")
            self._segment_available.clear()
            await asyncio.wait_for(self._segment_available.wait(), self.client.receive_timeout_seconds)
        try:
            return self._segment_receipts[previous_count]
        except IndexError as exc:
            raise MiniMaxTtsError("MiniMax TTS 分段回执缺失") from exc

    def _store_audio(self, pcm: bytes) -> None:
        if self.received_bytes + len(pcm) > self._cache_max_bytes:
            raise MiniMaxTtsError(
                f"MiniMax 音频缓存超过上限（{self._cache_max_bytes} bytes）"
            )
        try:
            if self._audio is None:
                self._audio = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
            self._audio.seek(self._audio_bytes_received)
            if self._audio.write(pcm) != len(pcm):
                raise OSError("short PCM cache write")
        except Exception as exc:
            raise MiniMaxTtsError(f"MiniMax 音频缓存写入失败：{exc}") from exc
        self._audio_bytes_received += len(pcm)
        self._cache_peak_bytes = max(self._cache_peak_bytes, self.received_bytes - self._read_offset)
        self._audio_available.set()

    def _release_audio(self) -> None:
        if self._audio is not None:
            self._audio.close()
            self._audio = None

    def _closed_error(self, operation: str) -> MiniMaxTtsError:
        return MiniMaxTtsError(f"MiniMax TTS 会话已关闭，无法{operation}")

    async def _send_bidi_event(self, payload: dict[str, Any], *, operation: str) -> None:
        """Send one frame without dereferencing a socket detached by ``close``.

        The lock covers only the local state check and one socket write.  In
        particular, callers must not hold it while waiting for a provider
        confirmation, because cancellation and replacement need to close a
        session immediately.
        """

        async with self._send_lock:
            if self._closed:
                raise self._closed_error(operation)
            socket = self._socket
            if socket is None:
                raise self._closed_error(operation)
            await socket.send(json.dumps(payload, ensure_ascii=False))

    async def start(self) -> None:
        # Prewarm and the first real text can race on the same retained
        # session.  Serialize the handshake so task_start is sent exactly
        # once and the caller either checks out a ready socket or waits for
        # the in-flight cold start to finish.
        async with self._start_lock:
            await self._start()

    async def _start(self) -> None:
        # ``session()`` returns the same object for Bidi.  A previous flush or
        # cancel ended only its generation, so starting the next one must not
        # send a second task_start or create a second receiver task.
        if self.connection_ready:
            if self.bidi and self._generation_done.is_set():
                self._reset_generation()
            return
        try:
            self._closed = False
            self._connection_dead = False
            # A manually closed session may be started again by a caller that
            # owns the object directly.  Drop stale task-local audio/events
            # before creating the replacement connection.
            if self._started or self._finished.is_set() or self._error is not None:
                self._reset_generation()
            import websockets  # type: ignore
        except ImportError as exc:
            raise MiniMaxTtsError("缺少 websockets，请先运行 scripts\\setup.ps1。") from exc
        headers = {"Authorization": f"Bearer {self.client.api_key}"}
        url = self.client.ws_url
        logger.info(
            "minimax.start url=%s model=%s format=%s sample_rate=%d",
            url,
            self.client.model,
            self.client.output_format,
            self.client.sample_rate,
        )
        try:
            try:
                self._socket = await websockets.connect(
                    url,
                    additional_headers=headers,
                    open_timeout=5,
                    close_timeout=1,
                    ping_interval=max(1, int(getattr(self.client, "keepalive_seconds", 30))),
                    # PCM backpressure can pause socket reads for longer than
                    # a pong deadline. Receive inactivity is bounded separately.
                    ping_timeout=None,
                )
            except TypeError:
                self._socket = await websockets.connect(
                    url,
                    extra_headers=headers,
                    open_timeout=5,
                    close_timeout=1,
                    ping_interval=max(1, int(getattr(self.client, "keepalive_seconds", 30))),
                    ping_timeout=None,
                )
            if self.bidi:
                # The Bidi endpoint announces the connection before it
                # accepts the task configuration.  Waiting for this frame
                # prevents task_start from racing the server handshake and
                # makes an out-of-order response an explicit protocol error.
                connected = self._parse(
                    await asyncio.wait_for(self._socket.recv(), self.client.timeout_seconds)
                )
                connected_event = connected.get("event")
                if connected_event == "task_failed":
                    code = self._error_code(connected)
                    message = self._error_message(connected)
                    if _retryable_status_code(code):
                        raise MiniMaxTtsConnectionError(message, retryable=True)
                    raise MiniMaxTtsError(message)
                if connected_event != "connected_success":
                    raise MiniMaxTtsError(
                        f"MiniMax Bidi 连接握手异常：期望 connected_success，收到 {connected_event or 'unknown'}"
                    )
                logger.debug("minimax.connected_success connect_id=%s", connected.get("connect_id"))
            await self._socket.send(json.dumps(self.client.start_payload(self.emotion), ensure_ascii=False))
            logger.debug("minimax.task_start_sent")
            while True:
                message = await asyncio.wait_for(self._socket.recv(), self.client.timeout_seconds)
                parsed = self._parse(message)
                event = parsed.get("event")
                if event == "task_failed":
                    message = self._error_message(parsed)
                    code = self._error_code(parsed)
                    if self.client.emotion_enabled and _is_emotion_rejection(message):
                        # The WebSocket public examples do not document this
                        # optional HTTP/MCP field.  Retry this output without
                        # it rather than leaving a rejected session retained.
                        self.client.disable_emotion_transport(message)
                        raise MiniMaxEmotionUnsupported(message)
                    if _retryable_status_code(code):
                        raise MiniMaxTtsConnectionError(message, retryable=True)
                    raise MiniMaxTtsError(message)
                if event == "task_started":
                    self._started = True
                    self.connection_id = self.client._next_connection_id()
                    self._generation_done.clear()
                    self._generation_active = False
                    logger.info("minimax.task_started")
                    break
            self._receiver = asyncio.create_task(self._receive_loop(), name="minimax-tts-receive")
        except MiniMaxTtsError:
            logger.exception("minimax.start_failed")
            await self.close()
            raise
        except Exception as exc:
            logger.exception("minimax.start_failed")
            await self.close()
            raise MiniMaxTtsConnectionError(
                f"MiniMax TTS 连接失败：{exc}",
                retryable=_retryable_connection_failure(exc),
            ) from exc

    @staticmethod
    def _parse(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise MiniMaxTtsError("MiniMax 返回的消息不是 JSON 对象")
        return parsed

    @staticmethod
    def _error_message(message: dict[str, Any]) -> str:
        base = message.get("base_resp")
        if isinstance(base, dict):
            code = base.get("status_code") or base.get("code")
            message_text = base.get("status_msg") or base.get("message")
            if message_text and code:
                return f"{message_text} ({code})"
            return str(message_text or code or "未知错误")
        code = message.get("status_code") or message.get("code")
        message_text = message.get("status_msg") or message.get("message") or message.get("error")
        if message_text and code:
            return f"{message_text} ({code})"
        return str(message_text or code or "未知错误")

    @staticmethod
    def _error_code(message: dict[str, Any]) -> str | None:
        base = message.get("base_resp")
        if isinstance(base, dict):
            value = base.get("status_code") or base.get("code")
            return str(value) if value is not None else None
        value = message.get("status_code") or message.get("code")
        return str(value) if value is not None else None

    @staticmethod
    def _decode_audio(audio: str) -> bytes:
        """Decode MiniMax's hex-encoded audio payload to raw audio bytes."""
        try:
            # MiniMax T2A WebSocket returns ``data.audio`` as a hex string,
            # including when ``audio_setting.format`` is ``pcm``.  It is not
            # Base64, so using base64.b64decode here turns the audio into
            # noise before it reaches the native PCM player.
            return bytes.fromhex(audio)
        except ValueError as exc:
            raise MiniMaxTtsError("MiniMax 音频字段不是合法的十六进制编码") from exc

    async def _receive_loop(self) -> None:
        audio_chunks = 0
        audio_bytes = 0
        try:
            while self._socket is not None:
                parsed = self._parse(await asyncio.wait_for(self._socket.recv(), self.client.receive_timeout_seconds))
                event = parsed.get("event")
                self._last_event = event if isinstance(event, str) else None
                # Keep protocol diagnostics without recording returned text or
                # PCM.  Audio responses are logged separately with counters.
                if event != "task_continued":
                    logger.debug(
                        "minimax.receive_event event=%s is_final=%s",
                        event,
                        parsed.get("is_final"),
                    )
                if event == "task_failed":
                    code = self._error_code(parsed)
                    rejected_text = self._rejected_text(parsed)
                    if self.bidi and code == "2205" and rejected_text:
                        try:
                            await self._retry_rejected_text(rejected_text)
                            continue
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self._error = MiniMaxTtsError(f"MiniMax 2205 重试失败：{exc}")
                            logger.error("minimax.backpressure_retry_failed message=%s", self._error)
                            break
                    if self.bidi and code == "2205":
                        self._error = MiniMaxTtsError(
                            "MiniMax 2205 未关联被拒绝文本，已终止当前 generation"
                        )
                        logger.error("minimax.backpressure_retry_unassociated")
                        break
                    self._error = MiniMaxTtsError(self._error_message(parsed))
                    logger.error("minimax.task_failed code=%s message=%s", code, self._error)
                    break
                cancel_confirmation = event == "task_canceled"
                if event == "task_canceled":
                    self._cancel_requested = False
                    self._generation_active = False
                    self._cancel_confirmed.set()
                    self._generation_done.set()
                    self._server_finished = False
                    # MiniMax has acknowledged the cancellation, but the
                    # Bidi endpoint does not guarantee that a cancelled
                    # task can accept another independent generation on the
                    # same socket.  Reusing it produced the observable
                    # failure mode ``task_flushed`` with zero PCM.  Treat a
                    # confirmed cancellation as a connection boundary: the
                    # owner closes this session and the next speak opens a
                    # fresh Bidi connection.
                    self._connection_dead = True
                    self._audio_available.set()
                    logger.info("minimax.task_canceled reusable=False")
                elif event == "task_flushed":
                    self._flush_confirmed.set()
                    self._generation_active = False
                    self._generation_done.set()
                    # A flush ends an independently authorised speech
                    # transaction.  Production traces show that this Bidi
                    # endpoint may acknowledge a following task_continue
                    # yet return zero PCM.  Do not carry an idle socket into
                    # the next speak: SpeechPipeline closes it after this
                    # generation and MiniMaxTtsClient opens a fresh one.
                    self._connection_dead = True
                    self._audio_available.set()
                    logger.info("minimax.task_flushed reusable=False")
                elif event == "sentence_start" and self._generation_active and not self._cancel_requested:
                    self._sentence_index += 1
                    self._sentence_audio_base_ms = self._received_audio_ms()
                    logger.debug(
                        "minimax.sentence_start index=%d audio_base_ms=%d",
                        self._sentence_index,
                        self._sentence_audio_base_ms,
                    )
                elif event == "sentence_end" and self._generation_active and not self._cancel_requested:
                    self._sentence_count += 1
                    self._sentence_available.set()
                if (not self.bidi or self._generation_active) and not self._cancel_requested and not cancel_confirmation:
                    alignment = self._extract_alignment(parsed)
                    if alignment is not None:
                        alignment["connection_id"] = self.connection_id
                        alignment["sentence_index"] = self._sentence_index
                        alignment["audio_base_ms"] = self._sentence_audio_base_ms
                        alignment["audio_returned_ms"] = self._received_audio_ms()
                        self._alignment_events.append(alignment)
                data = parsed.get("data")
                if (not self.bidi or self._generation_active) and not self._cancel_requested and not cancel_confirmation and isinstance(data, dict) and isinstance(data.get("audio"), str) and data["audio"]:
                    try:
                        pcm = self._decode_audio(data["audio"])
                        self._store_audio(pcm)
                        audio_chunks += 1
                        audio_bytes += len(pcm)
                        self._audio_chunks_received = audio_chunks
                        logger.debug(
                            "minimax.audio_chunk encoding=hex count=%d bytes=%d total_bytes=%d",
                            audio_chunks,
                            len(pcm),
                            audio_bytes,
                        )
                    except Exception as exc:
                        self._error = exc
                        logger.exception("minimax.audio_buffer_failed")
                        break
                if parsed.get("is_final") is True:
                    if (not self.bidi or self._generation_active) and not self._cancel_requested and not cancel_confirmation:
                        self._completed_segments += 1
                        text_chars = self._pending_segment_chars.popleft() if self._pending_segment_chars else 0
                        self._segment_receipts.append(
                            TtsSegmentReceipt(
                                index=self._completed_segments - 1,
                                text_chars=text_chars,
                                end_received_bytes=self._audio_bytes_received,
                            )
                        )
                        self._segment_available.set()
                        logger.debug(
                            "minimax.segment_finished count=%d text_chars=%d end_received_bytes=%d",
                            self.completed_segments,
                            text_chars,
                            self._audio_bytes_received,
                        )
                if event == "task_finished":
                    self._server_finished = True
                    self._generation_done.set()
                    break
        except asyncio.CancelledError:
            # ``close`` marks the transaction closed before cancelling this
            # task.  That is normal local resource cleanup after the speech
            # pipeline has drained its usable PCM; recording it as a receive
            # failure made every successful one-shot TTS look unhealthy.
            # A cancellation not initiated by local close remains an error:
            # it can still represent an aborted receiver or lost transport.
            if not self._closed:
                self._error = MiniMaxTtsError("MiniMax TTS 接收已取消，未确认任务完成")
            else:
                logger.debug("minimax.receive_cancelled_during_local_close")
            raise
        except Exception as exc:
            self._error = MiniMaxTtsError(f"MiniMax TTS 接收失败（未收到 task_finished）：{exc}")
            logger.exception("minimax.receive_failed")
        finally:
            if self.bidi and not self._closed:
                self._connection_dead = True
            self._backpressure_idle.set()
            self._finished.set()
            self._audio_available.set()
            self._segment_available.set()
            logger.info(
                "minimax.receive_done audio_chunks=%d audio_bytes=%d server_finished=%s cache_peak_bytes=%d error=%s",
                audio_chunks, audio_bytes, self.server_finished, self._cache_peak_bytes, self._error,
            )

    async def send_text(self, text: str) -> None:
        if not self._started or self._socket is None:
            raise MiniMaxTtsError("MiniMax TTS 任务尚未 started")
        await self._backpressure_idle.wait()
        if self._cancel_requested:
            try:
                await asyncio.wait_for(
                    self._cancel_confirmed.wait(),
                    float(getattr(self.client, "cancel_timeout_seconds", 2.0)),
                )
            except asyncio.TimeoutError as exc:
                raise MiniMaxTtsError("等待上一轮 Bidi 取消确认超时") from exc
        if self._generation_done.is_set():
            if not self.bidi:
                raise MiniMaxTtsError("MiniMax TTS 任务已结束")
            self._reset_generation()
        self._generation_active = True
        receiver = self._receiver
        if receiver is not None and receiver.done():
            if self._error is not None:
                raise self._error
            raise MiniMaxTtsError(
                f"MiniMax TTS 任务已结束（最后事件：{self._last_event or 'unknown'}）"
            )
        self._pending_segment_chars.append(len(text))
        try:
            await self._send_bidi_event(
                {"event": "task_continue", "text": text}, operation="发送文本"
            )
        except BaseException:
            # A cancellation may land after the frame reached the server and
            # its fast is_final already consumed this marker.  Preserve the
            # original transport exception instead of masking it with an
            # empty-deque error.
            if self._pending_segment_chars:
                self._pending_segment_chars.pop()
            raise
        self._sent_texts.append(text)
        logger.info("minimax.task_continue chars=%d text=%r", len(text), text[:120])

    async def flush(self) -> None:
        """Flush pending Bidi text while keeping the session open."""

        if not self.bidi:
            await self.finish()
            return
        if self._socket is None or not self._started:
            return
        await self._backpressure_idle.wait()
        self._flush_confirmed.clear()
        await self._send_bidi_event({"event": "task_flush"}, operation="flush")
        logger.info("minimax.task_flush_sent")
        # ``close`` deliberately detaches ``self._receiver`` before awaiting
        # its cancellation.  Retain the task that belongs to this flush so a
        # later polling iteration cannot dereference ``None``.
        receiver = self._receiver
        if receiver is not None:
            deadline = asyncio.get_running_loop().time() + float(
                getattr(self.client, "receive_timeout_seconds", 120.0)
            )
            while not self._flush_confirmed.is_set():
                if self._closed:
                    raise self._closed_error("等待 flush 确认")
                if self._error is not None:
                    raise self._error
                if receiver.done():
                    raise MiniMaxTtsError(
                        "MiniMax Bidi 接收任务已结束，未确认 task_flushed"
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise MiniMaxTtsError("MiniMax Bidi 未确认 task_flushed")
                try:
                    await asyncio.wait_for(
                        self._flush_confirmed.wait(), min(0.2, remaining)
                    )
                except asyncio.TimeoutError:
                    continue

    async def cancel(self, *, timeout_seconds: float | None = None) -> bool:
        """Cancel synthesis without closing a Bidi session.

        Audio already delivered by MiniMax remains in the local spool; the
        controller's generation gate is responsible for dropping it.
        """

        if not self.bidi or self._socket is None or not self._started:
            return False
        if self._cancel_requested:
            try:
                await self._wait_for_cancel_confirmation(timeout_seconds)
                return True
            except asyncio.TimeoutError as exc:
                raise MiniMaxTtsError("MiniMax Bidi 未在超时内确认 task_canceled") from exc
        self._cancel_confirmed.clear()
        self._cancel_requested = True
        self._retry_cancel.set()
        await self._send_bidi_event({"event": "task_cancel"}, operation="取消任务")
        timeout = timeout_seconds or float(getattr(self.client, "cancel_timeout_seconds", 2.0))
        if self._receiver is None:
            return True
        try:
            await self._wait_for_cancel_confirmation(timeout)
            return True
        except asyncio.TimeoutError as exc:
            raise MiniMaxTtsError("MiniMax Bidi 未在超时内确认 task_canceled") from exc

    async def _wait_for_cancel_confirmation(self, timeout: float | None = None) -> None:
        deadline = asyncio.get_running_loop().time() + float(
            timeout if timeout is not None else getattr(self.client, "cancel_timeout_seconds", 2.0)
        )
        receiver = self._receiver
        while not self._cancel_confirmed.is_set():
            if self._closed:
                raise self._closed_error("等待取消确认")
            if self._error is not None:
                raise self._error
            if receiver is not None and receiver.done():
                raise MiniMaxTtsError(
                    "MiniMax Bidi 接收任务已结束，未确认 task_canceled"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(
                    self._cancel_confirmed.wait(), min(0.2, remaining)
                )
            except asyncio.TimeoutError:
                continue

    async def finish(self) -> None:
        if self.server_finished:
            return
        if self._error is not None:
            raise self._error
        if self._socket is None:
            return
        self._finish_requested = True
        await self._send_bidi_event({"event": "task_finish"}, operation="结束任务")
        logger.info("minimax.task_finish_sent")

    async def audio_chunks(self):
        try:
            while not self._closed:
                if self._read_offset < self.received_bytes:
                    try:
                        self._audio.seek(self._read_offset)
                        pcm = self._audio.read(min(2048, self.received_bytes - self._read_offset))
                        if not pcm:
                            raise OSError("unexpected end of PCM cache")
                    except Exception as exc:
                        raise MiniMaxTtsError(f"MiniMax 音频缓存读取失败：{exc}") from exc
                    self._read_offset += len(pcm)
                    yield pcm
                elif self.bidi and self.generation_finished:
                    if self._error:
                        raise self._error
                    return
                elif self.receive_finished:
                    if self._error:
                        raise self._error
                    if not self.server_finished:
                        raise MiniMaxTtsError("MiniMax TTS 未收到 task_finished")
                    return
                else:
                    self._audio_available.clear()
                    await self._audio_available.wait()
        finally:
            if self.receive_finished or self._closed:
                self._release_audio()

    async def close(self) -> None:
        self._closed = True
        self._connection_dead = True
        self._prewarmed = False
        self._retry_cancel.set()
        receiver, self._receiver = self._receiver, None
        socket, self._socket = self._socket, None
        try:
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if socket is not None:
                await socket.close()
        finally:
            self._finished.set()
            self._audio_available.set()
            self._segment_available.set()
            self._release_audio()
            logger.info("minimax.closed received_bytes=%d consumed_bytes=%d", self.received_bytes, self._read_offset)

    def _reset_generation(self) -> None:
        """Reset task-local state while retaining the Bidi socket/receiver."""

        if not self.bidi:
            return
        self._release_audio()
        self._read_offset = 0
        self._cache_peak_bytes = 0
        self._audio_available.clear()
        self._server_finished = False
        self._completed_segments = 0
        self._pending_segment_chars.clear()
        self._segment_receipts.clear()
        self._segment_available.clear()
        self._finished.clear()
        self._error = None
        self._finish_requested = False
        self._audio_chunks_received = 0
        self._audio_bytes_received = 0
        self._last_event = None
        self._cancel_confirmed.clear()
        self._cancel_requested = False
        self._retry_cancel.clear()
        self._flush_confirmed.clear()
        self._sentence_available.clear()
        self._sentence_count = 0
        self._sentence_index = 0
        self._sentence_audio_base_ms = 0
        self._alignment_events.clear()
        self._retry_attempts.clear()
        self._retry_deadlines.clear()
        self._sent_texts.clear()
        self._backpressure_idle.set()
        self._generation_done.clear()
        self._generation_active = False

    @staticmethod
    def _rejected_text(message: dict[str, Any]) -> str | None:
        # A generic ``text`` field is commonly the human-readable error
        # message, not the task_continue payload that the service refused.
        # Retrying it would violate the no-guessing rule and could duplicate
        # an already accepted sentence.  Only explicitly named rejected
        # fields establish a safe association.
        for key in ("rejected_text", "failed_text"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        data = message.get("data")
        if isinstance(data, dict):
            for key in ("rejected_text", "failed_text"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    async def _retry_rejected_text(self, text: str) -> None:
        """Retry only a text explicitly named by a 2205 response.

        The provider does not expose a portable client-side message ID.  A
        response without the rejected text is therefore fatal and is handled
        by ``_receive_loop`` without guessing which accepted text to repeat.
        """

        occurrences = sum(item == text for item in self._sent_texts)
        if occurrences == 0:
            raise MiniMaxTtsError("2205 响应未关联到当前 generation 已发送的文本")
        if occurrences != 1:
            raise MiniMaxTtsError(
                "2205 响应无法唯一关联到当前 generation 的被拒绝文本"
            )
        attempt = self._retry_attempts.get(text, 0)
        loop = asyncio.get_running_loop()
        deadline = self._retry_deadlines.setdefault(text, loop.time() + 10.0)
        self._backpressure_idle.clear()
        try:
            while True:
                if self._cancel_requested or self._closed:
                    return
                delay = min(1.0, 0.1 * (2 ** attempt))
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise MiniMaxTtsError("2205 重试预算已耗尽")
                sleep_task = asyncio.create_task(asyncio.sleep(min(delay, remaining)))
                cancel_task = asyncio.create_task(self._retry_cancel.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {sleep_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in done:
                        return
                finally:
                    for task in (sleep_task, cancel_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sleep_task, cancel_task, return_exceptions=True)
                if self._cancel_requested or self._closed:
                    return
                await self._send_bidi_event(
                    {"event": "task_continue", "text": text}, operation="重试发送文本"
                )
                self._sent_texts.append(text)
                self._retry_attempts[text] = attempt + 1
                logger.info("minimax.task_continue_retry code=2205 attempt=%d chars=%d", attempt + 1, len(text))
                return
        finally:
            self._backpressure_idle.set()

    @staticmethod
    def _extract_alignment(message: dict[str, Any]) -> dict[str, Any] | None:
        """Find provider subtitle data without coupling the controller to wire names.

        The Bidi documentation advertises word_streaming subtitles, while
        deployments have returned the payload under slightly different keys.
        Keep the raw object and let the alignment layer validate its shape.
        """

        for key in ("subtitle", "subtitles", "alignment", "timestamps", "word_timestamps", "sentence"):
            value = message.get(key)
            if value is not None:
                return {"source": key, "value": value, "event": message.get("event")}
        data = message.get("data")
        if isinstance(data, dict):
            for key in ("subtitle", "subtitles", "alignment", "timestamps", "word_timestamps"):
                value = data.get(key)
                if value is not None:
                    return {"source": key, "value": value, "event": message.get("event")}
        return None


class MiniMaxTtsClient:
    def __init__(
        self,
        *,
        base_url: str,
        websocket_path: str,
        model: str,
        api_key: str,
        output_format: str = "pcm",
        sample_rate: int = 32000,
        voice_id: str = "female-shaonv",
        timeout_ms: int = 15000,
        transport: str = "websocket",
        scheduling: str = "auto",
        subtitle_enable: bool = False,
        subtitle_type: str = "word_streaming",
        audio_cache_max_bytes: int = 64 * 1024 * 1024,
        cancel_timeout_ms: int = 2000,
        keepalive_seconds: int = 30,
        emotion_mode: str = "agent",
        default_emotion: str = "calm",
        emotion_transport: str = "voice_setting",
        session_reuse: bool = True,
        admission_lead_ms: int = 2500,
        sentence_prefetch_ms: int = 2000,
        sentence_tick_ms: int = 50,
        first_clause_timeout_ms: int = 400,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if transport not in {"websocket", "websocket_bidi"}:
            raise ValueError("tts.transport must be websocket or websocket_bidi")
        if scheduling not in {"auto", "direct", "playback_window", "sentence_window"}:
            raise ValueError("tts.scheduling must be auto, direct, playback_window, or sentence_window")
        # Bidi may use playback_window for its lead window; only the legacy
        # per-segment completion wait is unavailable on that transport.
        if admission_lead_ms <= 0:
            raise ValueError("tts.admission_lead_ms must be positive")
        if sentence_prefetch_ms <= 0 or sentence_tick_ms <= 0 or first_clause_timeout_ms <= 0:
            raise ValueError("TTS sentence scheduling values must be positive")
        if audio_cache_max_bytes <= 0 or cancel_timeout_ms <= 0 or keepalive_seconds <= 0:
            raise ValueError("TTS cache, cancel timeout, and keepalive values must be positive")
        if emotion_mode not in {"off", "agent"}:
            raise ValueError("tts.emotion_mode must be off or agent")
        if emotion_transport != "voice_setting":
            raise ValueError("tts.emotion_transport must be voice_setting")
        self.transport = transport
        self.scheduling = scheduling
        self.admission_lead_ms = admission_lead_ms
        self.sentence_prefetch_ms = sentence_prefetch_ms
        self.sentence_tick_ms = sentence_tick_ms
        self.first_clause_timeout_ms = first_clause_timeout_ms
        if transport == "websocket_bidi" and websocket_path.rstrip("/") == "/ws/v1/t2a_v2":
            websocket_path = "/ws/v1/t2a_v2_bidi"
        self.websocket_path = websocket_path
        self.model = model
        self.api_key = api_key
        self.output_format = output_format
        self.sample_rate = sample_rate
        self.voice_id = voice_id
        self.subtitle_enable = subtitle_enable
        self.subtitle_type = subtitle_type
        self.audio_cache_max_bytes = audio_cache_max_bytes
        self.cancel_timeout_seconds = cancel_timeout_ms / 1000
        self.keepalive_seconds = keepalive_seconds
        self.emotion_mode = emotion_mode
        self.default_emotion = self.normalize_emotion(default_emotion)
        self.emotion_transport = emotion_transport
        self.session_reuse = bool(session_reuse)
        self.emotion_enabled = emotion_mode == "agent" and emotion_transport == "voice_setting"
        self.emotion_status = "requested" if self.emotion_enabled else "disabled"
        self.emotion_error: str | None = None
        self._active_emotion = self.default_emotion
        self.timeout_seconds = timeout_ms / 1000
        # ``timeout_ms`` is kept as the connection/task-start timeout.  The
        # service documents a 120-second idle window after the last result;
        # using the shorter startup timeout for every audio recv could cut a
        # valid long response during a temporary synthesis pause.
        self.receive_timeout_seconds = max(120.0, self.timeout_seconds)
        self._bidi_session: MiniMaxTtsSession | None = None
        self._prewarm_session: MiniMaxTtsSession | None = None
        self._prewarm_turn_id: str | None = None
        self.last_prewarm_checkout_reason = "not_requested"
        self._connection_sequence = 0

    def _next_connection_id(self) -> int:
        self._connection_sequence += 1
        return self._connection_sequence

    async def start_session_with_retry(
        self,
        session: MiniMaxTtsSession,
        *,
        max_attempts: int = 2,
    ) -> MiniMaxTtsSession:
        """Retry transient connection/start errors before any text is sent.

        A failed session is discarded, then the retry uses a fresh object so
        stale receiver state or PCM offsets cannot cross transaction boundaries.
        """

        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        active = session
        for attempt in range(max_attempts):
            try:
                await active.start()
                return active
            except MiniMaxTtsConnectionError as exc:
                if not exc.retryable or attempt + 1 >= max_attempts:
                    raise
                logger.warning(
                    "minimax.start_retry attempt=%d/%d error=%s",
                    attempt + 1,
                    max_attempts,
                    str(exc)[:240],
                )
                await active.close()
                if self._prewarm_session is active:
                    self._prewarm_session = None
                    self._prewarm_turn_id = None
                if self._bidi_session is active:
                    self._bidi_session = None
                await asyncio.sleep(0.25 * (2 ** attempt))
                active = self.session(fresh=True)
        return active

    @property
    def effective_scheduling(self) -> str:
        if self.transport == "websocket_bidi" and self.scheduling == "auto":
            return "direct"
        return self.scheduling

    @property
    def ws_url(self) -> str:
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}{self.websocket_path}"

    @staticmethod
    def normalize_emotion(value: str | None) -> str:
        allowed = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
        return value if isinstance(value, str) and value in allowed else "calm"

    def start_payload(self, emotion: str | None = None) -> dict[str, Any]:
        payload = {
            "event": "task_start",
            "model": self.model,
            "language_boost": "Chinese",
            "voice_setting": {
                "voice_id": self.voice_id,
                "speed": 1,
                "vol": 1,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": self.sample_rate,
                "bitrate": 128000,
                "format": self.output_format,
                "channel": 1,
            },
        }
        selected = self.normalize_emotion(emotion or self._active_emotion)
        if self.emotion_enabled:
            payload["voice_setting"]["emotion"] = selected
        if self.transport == "websocket_bidi" or self.subtitle_enable:
            payload["subtitle_enable"] = bool(self.subtitle_enable)
            payload["subtitle_type"] = self.subtitle_type
        return payload

    async def prepare_emotion(self, emotion: str | None, *, detach_active: bool = False) -> str:
        """Select a voice setting before a Bidi task starts.

        ``task_start`` owns MiniMax's voice settings, so a retained Bidi
        connection can only be reused when the selected emotion is unchanged.
        Callers invoke this between output transactions, after their cancel
        barrier has joined the preceding task.
        """
        selected = self.normalize_emotion(emotion)
        if self.emotion_mode != "agent":
            selected = self.default_emotion
        if self.transport == "websocket" and self._prewarm_session is not None:
            if detach_active or self._prewarm_session.emotion != selected:
                session = self._prewarm_session
                self._prewarm_session = None
                self._prewarm_turn_id = None
                start_lock = getattr(session, "_start_lock", None)
                if start_lock is not None:
                    async with start_lock:
                        await session.close()
                else:
                    await session.close()
        if self.transport == "websocket_bidi" and self._bidi_session is not None and detach_active:
            # The output owner has already transferred cancellation ownership
            # to a retired generation.  Detach its reusable pointer instead
            # of awaiting its socket close on the replacement's critical
            # path; that old handle will close it in background cleanup.
            self._bidi_session = None
        elif self.transport == "websocket_bidi" and self._bidi_session is not None and selected != self._active_emotion:
            session = self._bidi_session
            # A prewarm may still be in the handshake.  Wait for the session
            # start lock before closing it so a mismatched-emotion checkout
            # cannot race a socket assignment and leak the new connection.
            start_lock = getattr(session, "_start_lock", None)
            if start_lock is not None:
                async with start_lock:
                    await session.close()
            else:
                await session.close()
            self._bidi_session = None
        self._active_emotion = selected
        return selected

    def disable_emotion_transport(self, error: str) -> None:
        """Make later Bidi sessions neutral after a server-side rejection."""

        self.emotion_enabled = False
        self.emotion_status = "unsupported"
        self.emotion_error = error[:300]
        self._active_emotion = self.default_emotion
        logger.warning("minimax.emotion_unsupported error=%s", self.emotion_error)

    @property
    def applied_emotion(self) -> str:
        return self._active_emotion if self.emotion_enabled else self.default_emotion

    def session(self, *, fresh: bool = False, turn_id: str | None = None) -> MiniMaxTtsSession:
        if not self.api_key:
            raise MiniMaxTtsError("未配置 MINIMAX_API_KEY")
        if self.output_format.lower() != "pcm":
            raise MiniMaxTtsError("当前原生播放器只接受 PCM，未启用 MP3 自动回退。")
        # A replacement must receive a socket that is not still draining a
        # cancel/flush of the previous generation.  Do not publish this
        # isolated transaction as the reusable idle session.
        if fresh:
            isolated = MiniMaxTtsSession(self, emotion=self._active_emotion)
            isolated._isolated_transaction = True
            return isolated
        if self.transport == "websocket" and self.session_reuse:
            if (
                self._prewarm_session is not None
                and (self._prewarm_turn_id is None or self._prewarm_turn_id == turn_id)
            ):
                return self._prewarm_session
        if self.transport == "websocket_bidi" and self.session_reuse:
            if (
                self._bidi_session is None
                or self._bidi_session.closed
                or self._bidi_session._connection_dead
                or self._bidi_session._error is not None
                or (self._bidi_session._started and self._bidi_session._socket is None)
            ):
                self._bidi_session = MiniMaxTtsSession(self, emotion=self._active_emotion)
            return self._bidi_session
        return MiniMaxTtsSession(self, emotion=self._active_emotion)

    async def prewarm(self, emotion: str | None = None,
                      turn_id: str | None = None) -> dict[str, Any]:
        """Open an idle task_start session without sending any text.

        The returned session is deliberately only a checkout candidate.  It
        has not synthesized audio and becomes consumed as soon as the first
        real speech transaction claims it.  Standard
        WebSocket candidates are scoped to one turn and consumed on checkout.
        """

        if self.transport not in {"websocket", "websocket_bidi"} or not self.session_reuse:
            return {"status": "DISABLED", "reason": "session_prewarm_unavailable"}
        selected = self.normalize_emotion(emotion or self.default_emotion)
        selected = await self.prepare_emotion(selected)
        if self.transport == "websocket":
            if self._prewarm_session is not None:
                if self._prewarm_turn_id == turn_id and not self._prewarm_session.closed:
                    session = self._prewarm_session
                else:
                    stale = self._prewarm_session
                    self._prewarm_session = None
                    self._prewarm_turn_id = None
                    await stale.close()
                    session = MiniMaxTtsSession(self, emotion=selected)
            else:
                session = MiniMaxTtsSession(self, emotion=selected)
            self._prewarm_session = session
            self._prewarm_turn_id = turn_id
        else:
            session = self.session(turn_id=turn_id)
        # Emotion is part of task_start, so a concurrent Agent decision may
        # have selected a different voice between prepare_emotion() and
        # session().  Never label that connection as a calm warm candidate;
        # the real output path will create a correctly configured cold one.
        if session.emotion != selected:
            if session is self._bidi_session and not session.generation_active:
                await session.close()
                self._bidi_session = None
            if session is self._prewarm_session:
                await session.close()
                self._prewarm_session = None
                self._prewarm_turn_id = None
            return {"status": "FALLBACK", "reason": "emotion_changed"}
        if session.generation_active:
            return {"status": "FALLBACK", "reason": "session_busy"}
        session._prewarm_inflight = True
        session._prewarm_ready.clear()
        try:
            await session.start()
            retained = session is (
                self._bidi_session if self.transport == "websocket_bidi" else self._prewarm_session
            )
            usable = session.reusable if self.transport == "websocket_bidi" else session.connection_ready
            if not retained or session.emotion != self._active_emotion or not usable:
                await session.close()
                if session is self._bidi_session:
                    self._bidi_session = None
                if session is self._prewarm_session:
                    self._prewarm_session = None
                    self._prewarm_turn_id = None
                return {"status": "FALLBACK", "reason": "session_not_reusable"}
            session._prewarmed = True
            logger.info("minimax.prewarm_ready connection_id=%s emotion=%s", session.connection_id, selected)
            return {"status": "READY", "connection_id": session.connection_id}
        except asyncio.CancelledError:
            await session.close()
            if session is self._bidi_session:
                self._bidi_session = None
            if session is self._prewarm_session:
                self._prewarm_session = None
                self._prewarm_turn_id = None
            raise
        except MiniMaxEmotionUnsupported as exc:
            # ``start`` has already closed the rejected socket and disabled
            # the optional emotion extension.  A future real speak will use
            # the normal neutral cold-start fallback.
            logger.info("minimax.prewarm_emotion_fallback error=%s", str(exc)[:300])
            if session is self._prewarm_session:
                self._prewarm_session = None
                self._prewarm_turn_id = None
            return {"status": "FALLBACK", "reason": "emotion_unsupported"}
        except Exception as exc:
            logger.info("minimax.prewarm_failed error=%s", str(exc)[:300])
            if session is self._prewarm_session:
                self._prewarm_session = None
                self._prewarm_turn_id = None
            return {"status": "FALLBACK", "reason": str(exc)[:300]}
        finally:
            session._prewarm_inflight = False
            session._prewarm_ready.set()

    def checkout_prewarm(self, session: MiniMaxTtsSession,
                         turn_id: str | None = None) -> bool:
        """Claim a ready warm session exactly once for real speech."""
        retained = self._bidi_session if self.transport == "websocket_bidi" else self._prewarm_session
        if session is not retained:
            self.last_prewarm_checkout_reason = "stale_generation"
            return False
        if self.transport == "websocket" and self._prewarm_turn_id not in {None, turn_id}:
            self.last_prewarm_checkout_reason = "wrong_turn"
            return False
        if not getattr(session, "_prewarmed", False):
            self.last_prewarm_checkout_reason = "not_prewarmed"
            return False
        if session.emotion != self._active_emotion:
            self.last_prewarm_checkout_reason = "emotion_changed"
            return False
        usable = session.reusable if self.transport == "websocket_bidi" else session.connection_ready
        if not usable or session.closed or session._connection_dead:
            self.last_prewarm_checkout_reason = "session_closed"
            return False
        if session.generation_active:
            self.last_prewarm_checkout_reason = "session_busy"
            return False
        session._prewarmed = False
        if self.transport == "websocket":
            self._prewarm_session = None
            self._prewarm_turn_id = None
        self.last_prewarm_checkout_reason = "hit"
        return True

    async def discard_prewarm(self, turn_id: str | None = None) -> None:
        """Close an unused warm candidate owned by a cancelled turn."""

        if self.transport == "websocket":
            if self._prewarm_session is None:
                return
            if turn_id is not None and self._prewarm_turn_id != turn_id:
                return
            session = self._prewarm_session
            self._prewarm_session = None
            self._prewarm_turn_id = None
            if getattr(session, "_prewarmed", False) or getattr(session, "_prewarm_inflight", False):
                await session.close()
            return
        session = self._bidi_session
        if session is not None and (
            getattr(session, "_prewarmed", False) or getattr(session, "_prewarm_inflight", False)
        ):
            await session.close()
            if session is self._bidi_session:
                self._bidi_session = None

    async def close(self) -> None:
        """Close retained Bidi and unused standard prewarm connections."""

        if self._bidi_session is not None:
            await self._bidi_session.close()
            self._bidi_session = None
        if self._prewarm_session is not None:
            await self._prewarm_session.close()
            self._prewarm_session = None
            self._prewarm_turn_id = None

    async def check(self) -> None:
        if not self.api_key:
            raise MiniMaxTtsError("未配置 MINIMAX_API_KEY")
        if self.output_format.lower() != "pcm":
            raise MiniMaxTtsError("TTS output_format 必须为 pcm")
