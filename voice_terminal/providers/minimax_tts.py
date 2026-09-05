from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MiniMaxTtsError(RuntimeError):
    pass


_AUDIO_END = object()


class MiniMaxTtsSession:
    def __init__(self, client: "MiniMaxTtsClient") -> None:
        self.client = client
        self._socket: Any = None
        self._receiver: asyncio.Task[None] | None = None
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue()
        self._finished = asyncio.Event()
        self._started = False
        self._error: Exception | None = None
        self._send_lock = asyncio.Lock()

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
                    ping_interval=20,
                )
            except TypeError:
                self._socket = await websockets.connect(
                    url,
                    extra_headers=headers,
                    open_timeout=5,
                    ping_interval=20,
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
                parsed = self._parse(await self._socket.recv())
                event = parsed.get("event")
                if event == "task_failed":
                    self._error = MiniMaxTtsError(self._error_message(parsed))
                    logger.error("minimax.task_failed message=%s", self._error)
                    break
                data = parsed.get("data")
                if isinstance(data, dict) and isinstance(data.get("audio"), str) and data["audio"]:
                    try:
                        pcm = self._decode_audio(data["audio"])
                        audio_chunks += 1
                        audio_bytes += len(pcm)
                        logger.debug(
                            "minimax.audio_chunk encoding=hex count=%d bytes=%d total_bytes=%d",
                            audio_chunks,
                            len(pcm),
                            audio_bytes,
                        )
                        await self._audio.put(pcm)
                    except Exception as exc:
                        self._error = MiniMaxTtsError(f"MiniMax 音频解码失败：{exc}")
                        logger.exception("minimax.audio_decode_failed")
                        break
                if event == "task_finished" or parsed.get("is_final") is True:
                    if event == "task_finished":
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = MiniMaxTtsError(f"MiniMax TTS 接收失败：{exc}")
            logger.exception("minimax.receive_failed")
        finally:
            self._finished.set()
            await self._audio.put(_AUDIO_END)
            logger.info("minimax.receive_done audio_chunks=%d audio_bytes=%d", audio_chunks, audio_bytes)

    async def send_text(self, text: str) -> None:
        if not self._started or self._socket is None:
            raise MiniMaxTtsError("MiniMax TTS 任务尚未 started")
        async with self._send_lock:
            await self._socket.send(json.dumps({"event": "task_continue", "text": text}, ensure_ascii=False))
        logger.info("minimax.task_continue chars=%d text=%r", len(text), text[:120])

    async def finish(self) -> None:
        if self._socket is None:
            return
        async with self._send_lock:
            await self._socket.send(json.dumps({"event": "task_finish"}))
        logger.info("minimax.task_finish_sent")

    async def audio_chunks(self):
        while True:
            item = await self._audio.get()
            if item is _AUDIO_END:
                if self._error:
                    raise self._error
                return
            yield item  # type: ignore[misc]

    async def close(self) -> None:
        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()
        logger.info("minimax.closed")


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
