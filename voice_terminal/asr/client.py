from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import AudioChunk

logger = logging.getLogger(__name__)


class AsrWorkerError(RuntimeError):
    pass


ResultCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AsrWorkerClient:
    def __init__(self, url: str, *, on_result: ResultCallback | None = None) -> None:
        self.url = url
        self.on_result = on_result
        self._socket: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._sent_count_by_turn: dict[str, int] = {}
        self._sent_bytes_by_turn: dict[str, int] = {}
        self._last_log_at_by_turn: dict[str, float] = {}

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            import websockets  # type: ignore

            self._socket = await websockets.connect(self.url, open_timeout=5, ping_interval=20)
            self._receive_task = asyncio.create_task(self._receive_loop(), name="asr-worker-receive")
            logger.info("asr.client.connected url=%s", self.url)
        except Exception as exc:
            self._socket = None
            logger.exception("asr.client.connect_failed url=%s", self.url)
            raise AsrWorkerError(f"ASR Worker 连接失败：{exc}") from exc

    async def _receive_loop(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self.on_result is not None and isinstance(message, dict):
                    logger.info(
                        "asr.client.received type=%s turn_id=%s text_len=%d text=%r",
                        message.get("type"),
                        message.get("turn_id"),
                        len(message.get("text", "")) if isinstance(message.get("text"), str) else 0,
                        message.get("text", "")[:120] if isinstance(message.get("text"), str) else "",
                    )
                    try:
                        await self.on_result(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # A controller callback bug must not be mislabeled as
                        # a transport disconnect or tear down the ASR socket.
                        logger.exception("asr.client.result_callback_failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("asr.client.receive_failed")
            if self.on_result is not None:
                await self.on_result({"type": "error", "code": "ASR_WORKER_DISCONNECTED", "message": str(exc)})
        finally:
            if self._socket is socket:
                self._socket = None
            logger.info("asr.client.disconnected url=%s", self.url)

    async def _send(self, payload: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise AsrWorkerError("ASR Worker 尚未连接")
        async with self._send_lock:
            await socket.send(json.dumps(payload, ensure_ascii=False))
        logger.debug(
            "asr.client.sent type=%s turn_id=%s bytes=%d final=%s",
            payload.get("type"),
            payload.get("turn_id"),
            len(payload.get("pcm_base64", "")) if isinstance(payload.get("pcm_base64"), str) else 0,
            payload.get("is_final") is True,
        )

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._send(chunk.to_dict())
        turn_id = chunk.turn_id
        self._sent_count_by_turn[turn_id] = self._sent_count_by_turn.get(turn_id, 0) + 1
        self._sent_bytes_by_turn[turn_id] = self._sent_bytes_by_turn.get(turn_id, 0) + len(chunk.pcm)
        now = time.monotonic()
        last_log_at = self._last_log_at_by_turn.get(turn_id, 0.0)
        if chunk.is_final or not last_log_at or now - last_log_at >= 1.0:
            logger.info(
                "asr.client.audio_progress turn_id=%s chunk_count=%d bytes=%d final=%s",
                turn_id,
                self._sent_count_by_turn[turn_id],
                self._sent_bytes_by_turn[turn_id],
                chunk.is_final,
            )
            self._last_log_at_by_turn[turn_id] = now

    async def send_finish(self, turn_id: str) -> None:
        await self._send({"type": "turn.finish", "turn_id": turn_id})
        logger.info(
            "asr.client.turn_finish turn_id=%s chunks=%d bytes=%d",
            turn_id,
            self._sent_count_by_turn.pop(turn_id, 0),
            self._sent_bytes_by_turn.pop(turn_id, 0),
        )
        self._last_log_at_by_turn.pop(turn_id, None)

    async def health(self) -> bool:
        try:
            await self._send({"type": "health"})
            return True
        except AsrWorkerError:
            return False

    async def close(self) -> None:
        task, self._receive_task = self._receive_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()
        logger.info("asr.client.closed url=%s", self.url)
