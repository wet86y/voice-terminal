from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


class MiniMaxTtsError(RuntimeError):
    pass


class MiniMaxTtsSession:
    def __init__(self, client: "MiniMaxTtsClient") -> None:
        self.client = client
        self._socket: Any = None
        self._receiver: asyncio.Task[None] | None = None
        # One event-loop owns the file and its independent read/write offsets.
        # Receiving never waits for playback; long answers spill to disk.
        self._audio = None
        self._read_offset = 0
        self._cache_peak_bytes = 0
        self._audio_available = asyncio.Event()
        self._closed = False
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

    async def wait_for_segment(self, previous_count: int) -> None:
        """Wait for this text's is_final, never infer it from another PCM chunk."""
        while self.completed_segments <= previous_count:
            if self._error is not None:
                raise self._error
            if self.receive_finished or self._closed:
                raise MiniMaxTtsError("MiniMax TTS 未确认当前文本分段完成")
            self._segment_available.clear()
            await asyncio.wait_for(self._segment_available.wait(), self.client.receive_timeout_seconds)

    def _store_audio(self, pcm: bytes) -> None:
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

    async def start(self) -> None:
        try:
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
                    ping_interval=20,
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
                    ping_interval=20,
                    ping_timeout=None,
                )
            await self._socket.send(json.dumps(self.client.start_payload(), ensure_ascii=False))
            logger.debug("minimax.task_start_sent")
            while True:
                message = await asyncio.wait_for(self._socket.recv(), self.client.timeout_seconds)
                parsed = self._parse(message)
                event = parsed.get("event")
                if event == "task_failed":
                    raise MiniMaxTtsError(self._error_message(parsed))
                if event == "task_started":
                    self._started = True
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
            raise MiniMaxTtsError(f"MiniMax TTS 连接失败：{exc}") from exc

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
            return str(base.get("status_msg") or base.get("status_code") or "未知错误")
        return "未知错误"

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
                    self._error = MiniMaxTtsError(self._error_message(parsed))
                    logger.error("minimax.task_failed message=%s", self._error)
                    break
                data = parsed.get("data")
                if isinstance(data, dict) and isinstance(data.get("audio"), str) and data["audio"]:
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
                    self._completed_segments += 1
                    self._segment_available.set()
                    logger.debug("minimax.segment_finished count=%d", self.completed_segments)
                if event == "task_finished":
                    self._server_finished = True
                    break
        except asyncio.CancelledError:
            self._error = MiniMaxTtsError("MiniMax TTS 接收已取消，未确认任务完成")
            raise
        except Exception as exc:
            self._error = MiniMaxTtsError(f"MiniMax TTS 接收失败（未收到 task_finished）：{exc}")
            logger.exception("minimax.receive_failed")
        finally:
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
        if self._receiver is not None and self._receiver.done():
            if self._error is not None:
                raise self._error
            raise MiniMaxTtsError(
                f"MiniMax TTS 任务已结束（最后事件：{self._last_event or 'unknown'}）"
            )
        async with self._send_lock:
            await self._socket.send(json.dumps({"event": "task_continue", "text": text}, ensure_ascii=False))
        logger.info("minimax.task_continue chars=%d text=%r", len(text), text[:120])

    async def finish(self) -> None:
        if self.server_finished:
            return
        if self._error is not None:
            raise self._error
        if self._socket is None:
            return
        self._finish_requested = True
        async with self._send_lock:
            await self._socket.send(json.dumps({"event": "task_finish"}))
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.websocket_path = websocket_path
        self.model = model
        self.api_key = api_key
        self.output_format = output_format
        self.sample_rate = sample_rate
        self.voice_id = voice_id
        self.timeout_seconds = timeout_ms / 1000
        # ``timeout_ms`` is kept as the connection/task-start timeout.  The
        # service documents a 120-second idle window after the last result;
        # using the shorter startup timeout for every audio recv could cut a
        # valid long response during a temporary synthesis pause.
        self.receive_timeout_seconds = max(120.0, self.timeout_seconds)

    @property
    def ws_url(self) -> str:
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}{self.websocket_path}"

    def start_payload(self) -> dict[str, Any]:
        return {
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

    def session(self) -> MiniMaxTtsSession:
        if not self.api_key:
            raise MiniMaxTtsError("未配置 MINIMAX_API_KEY")
        if self.output_format.lower() != "pcm":
            raise MiniMaxTtsError("当前原生播放器只接受 PCM，未启用 MP3 自动回退。")
        return MiniMaxTtsSession(self)

    async def check(self) -> None:
        if not self.api_key:
            raise MiniMaxTtsError("未配置 MINIMAX_API_KEY")
        if self.output_format.lower() != "pcm":
            raise MiniMaxTtsError("TTS output_format 必须为 pcm")
