from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import asr_result, decode_audio_chunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AsrInferenceResult:
    """Cumulative transcript plus whether this call emitted new model text."""

    text: str
    emitted: bool


class AsrWorkerEngine:
    def __init__(
        self,
        *,
        model_dir: Path,
        device: str,
        sample_rate: int = 16000,
        chunk_size: tuple[int, int, int] = (0, 10, 5),
        encoder_chunk_look_back: int = 4,
        decoder_chunk_look_back: int = 1,
    ) -> None:
        self.model_dir = model_dir
        self.device = device
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.encoder_chunk_look_back = encoder_chunk_look_back
        self.decoder_chunk_look_back = decoder_chunk_look_back
        self._model: Any = None
        self._np: Any = None
        self._cache_by_turn: dict[str, dict[str, Any]] = {}
        # paraformer-zh-streaming emits text for the current streaming
        # window, not a complete transcript for the whole turn.  Keep the
        # cumulative transcript separately so later windows cannot replace
        # earlier words with only the last emitted characters.
        self._transcript_by_turn: dict[str, str] = {}
        self._pending_audio_by_turn: dict[str, bytearray] = {}
        self._chunk_count_by_turn: dict[str, int] = {}
        self._byte_count_by_turn: dict[str, int] = {}
        self._model_call_count_by_turn: dict[str, int] = {}
        self._last_log_at_by_turn: dict[str, float] = {}

    @property
    def chunk_stride_samples(self) -> int:
        """Return FunASR's streaming input window in samples.

        FunASR documents ``chunk_size[1] * 960`` as the input stride for
        paraformer-zh-streaming.  With the default ``[0, 10, 5]`` config at
        16 kHz this is 9,600 samples, or 600 ms of mono PCM.
        """

        middle_chunks = self.chunk_size[1] if len(self.chunk_size) > 1 else 10
        return max(1, int(self.sample_rate * middle_chunks * 60 / 1000))

    @property
    def chunk_stride_bytes(self) -> int:
        # The Worker accepts mono signed 16-bit PCM.
        return self.chunk_stride_samples * 2

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import numpy as np  # type: ignore
            from funasr import AutoModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 numpy/funasr，请先运行 scripts\\setup.ps1。") from exc
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"Paraformer 模型目录不存在：{self.model_dir}")
        self._np = np
        self._model = AutoModel(
            model=str(self.model_dir),
            device=self.device,
            disable_update=True,
            disable_pbar=True,
        )
        logger.info("asr.worker.model_ready model_dir=%s device=%s", self.model_dir, self.device)

    def _infer_sync(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        self.load()
        if len(pcm) % 2:
            pcm = pcm[:-1]
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        cache = self._cache_by_turn.setdefault(turn_id, {})
        result = self._model.generate(
            input=audio,
            cache=cache,
            is_final=is_final,
            chunk_size=list(self.chunk_size),
            encoder_chunk_look_back=self.encoder_chunk_look_back,
            decoder_chunk_look_back=self.decoder_chunk_look_back,
        )
        text = ""
        if isinstance(result, list) and result and isinstance(result[0], dict):
            value = result[0].get("text")
            if isinstance(value, str):
                text = value
        # Do not strip each window before concatenating.  A leading space can
        # be meaningful at an English word boundary; trim only when the
        # cumulative transcript is published.
        return text

    def _clear_turn(self, turn_id: str) -> None:
        self._cache_by_turn.pop(turn_id, None)
        self._transcript_by_turn.pop(turn_id, None)
        self._pending_audio_by_turn.pop(turn_id, None)
        self._chunk_count_by_turn.pop(turn_id, None)
        self._byte_count_by_turn.pop(turn_id, None)
        self._model_call_count_by_turn.pop(turn_id, None)
        self._last_log_at_by_turn.pop(turn_id, None)

    async def _infer_window(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        """Run one model window and return the text emitted by that window."""

        self._model_call_count_by_turn[turn_id] = self._model_call_count_by_turn.get(turn_id, 0) + 1
        return await asyncio.to_thread(self._infer_sync, turn_id, pcm, is_final)

    async def infer(self, turn_id: str, pcm: bytes, is_final: bool) -> AsrInferenceResult:
        started = time.perf_counter()
        self._chunk_count_by_turn[turn_id] = self._chunk_count_by_turn.get(turn_id, 0) + 1
        self._byte_count_by_turn[turn_id] = self._byte_count_by_turn.get(turn_id, 0) + len(pcm)

        pending = self._pending_audio_by_turn.setdefault(turn_id, bytearray())
        pending.extend(pcm)
        emitted_parts: list[str] = []

        if is_final:
            # Normally the final call contains only the remaining partial
            # window.  If the transport ever batches more than one window,
            # flush complete windows first and mark only the last call final.
            while len(pending) > self.chunk_stride_bytes:
                window = bytes(pending[: self.chunk_stride_bytes])
                del pending[: self.chunk_stride_bytes]
                emitted_parts.append(await self._infer_window(turn_id, window, False))
            if pending:
                window = bytes(pending)
                pending.clear()
                emitted_parts.append(await self._infer_window(turn_id, window, True))
        else:
            # Microphone callbacks arrive every 20 ms, while FunASR's
            # paraformer streaming API expects a much larger model window
            # (600 ms with the default [0, 10, 5] setting).
            while len(pending) >= self.chunk_stride_bytes:
                window = bytes(pending[: self.chunk_stride_bytes])
                del pending[: self.chunk_stride_bytes]
                emitted_parts.append(await self._infer_window(turn_id, window, False))

        emitted_text = "".join(emitted_parts)
        if emitted_text:
            self._transcript_by_turn[turn_id] = self._transcript_by_turn.get(turn_id, "") + emitted_text
        text = self._transcript_by_turn.get(turn_id, "").strip()
        elapsed_ms = (time.perf_counter() - started) * 1000
        now = time.monotonic()
        last_log_at = self._last_log_at_by_turn.get(turn_id, 0.0)
        if is_final or emitted_text or not last_log_at or now - last_log_at >= 1.0:
            logger.info(
                "asr.worker.infer turn_id=%s chunk_count=%d bytes=%d final=%s pending_bytes=%d model_calls=%d elapsed_ms=%.1f emitted_len=%d transcript_len=%d text=%r",
                turn_id,
                self._chunk_count_by_turn[turn_id],
                self._byte_count_by_turn[turn_id],
                is_final,
                len(pending),
                self._model_call_count_by_turn.get(turn_id, 0),
                elapsed_ms,
                len(emitted_text),
                len(text),
                text[:120],
            )
            self._last_log_at_by_turn[turn_id] = now
        else:
            logger.debug(
                "asr.worker.infer_chunk turn_id=%s bytes=%d final=%s pending_bytes=%d elapsed_ms=%.1f transcript_len=%d",
                turn_id,
                len(pcm),
                is_final,
                len(pending),
                elapsed_ms,
                len(text),
            )
        if is_final:
            # Return the final cumulative text before releasing per-turn
            # model cache and aggregation state.
            self._clear_turn(turn_id)
        return AsrInferenceResult(text=text, emitted=bool(emitted_text))


async def handle_asr_connection(websocket: Any, engine: AsrWorkerEngine) -> None:
    peer = getattr(websocket, "remote_address", None)
    logger.info("asr.worker.connection_open peer=%s", peer)
    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                logger.warning("asr.worker.binary_message_ignored bytes=%d", len(raw))
                continue
            message: dict[str, Any] = {}
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("消息必须是 JSON 对象")
                message_type = message.get("type")
                if message_type == "health":
                    engine.load()
                    await websocket.send(json.dumps({"type": "health", "status": "READY"}))
                    logger.info("asr.worker.health_reply")
                    continue
                if message_type == "turn.finish":
                    turn_id = message.get("turn_id")
                    if isinstance(turn_id, str):
                        engine._clear_turn(turn_id)
                        logger.info("asr.worker.turn_finish turn_id=%s", turn_id)
                    continue
                if message_type != "audio.chunk":
                    raise ValueError(f"不支持的 ASR 消息类型：{message_type}")
                chunk = decode_audio_chunk(message)
                logger.debug(
                    "asr.worker.audio_chunk turn_id=%s bytes=%d final=%s",
                    chunk.turn_id,
                    len(chunk.pcm),
                    chunk.is_final,
                )
                inference = await engine.infer(chunk.turn_id, chunk.pcm, chunk.is_final)
                # A final message is a protocol acknowledgement even when the
                # recognizer has no text.  Otherwise the Voice Server waits
                # until its timeout and cannot decide whether to continue.
                if inference.emitted or chunk.is_final:
                    result_type = "asr.final" if chunk.is_final else "asr.partial"
                    await websocket.send(
                        json.dumps(asr_result(result_type, chunk.turn_id, inference.text), ensure_ascii=False)
                    )
                    logger.info(
                        "asr.worker.result turn_id=%s type=%s text_len=%d text=%r",
                        chunk.turn_id,
                        result_type,
                        len(inference.text),
                        inference.text[:120],
                    )
            except Exception as exc:
                turn_id = message.get("turn_id") if isinstance(message, dict) else None
                logger.exception("asr.worker.message_failed turn_id=%s", turn_id)
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "ASR_INFERENCE_FAILED",
                            "message": str(exc),
                            "turn_id": turn_id if isinstance(turn_id, str) else None,
                        },
                        ensure_ascii=False,
                    )
                )
    finally:
        logger.info("asr.worker.connection_closed peer=%s", peer)
