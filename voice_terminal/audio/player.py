from __future__ import annotations

import asyncio
from collections import deque
import logging
import threading
from typing import Any, Callable

from .devices import resolve_device

logger = logging.getLogger(__name__)


class AudioPlayerError(RuntimeError):
    pass


class PcmPlayer:
    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        output_device: str | int | None,
        on_playback_started: Callable[[str], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.output_device = output_device
        self.on_playback_started = on_playback_started
        # The callback can consume only part of a TTS chunk.  A queue.Queue
        # cannot put that remainder at the front, so using it directly would
        # reorder audio as ``chunk-1 prefix, chunk-2, chunk-1 remainder``.
        # Keep a deque and append partial chunks back with appendleft().
        self._chunks: deque[bytes] = deque()
        self._queue_lock = threading.Lock()
        self._stream: Any = None
        self._active_turn: str | None = None
        self._started_for_turn: str | None = None
        self._buffered_bytes = 0

    @property
    def buffered_ms(self) -> int:
        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            buffered_bytes = self._buffered_bytes
        return int(buffered_bytes * 1000 / bytes_per_second) if bytes_per_second else 0

    @property
    def running(self) -> bool:
        return self._stream is not None

    def _callback(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        target = memoryview(outdata).cast("B")
        target[:] = b"\x00" * len(target)
        remaining = len(target)
        offset = 0
        playback_turn: str | None = None

        with self._queue_lock:
            while remaining > 0 and self._chunks:
                chunk = self._chunks.popleft()
                take = min(remaining, len(chunk))
                target[offset : offset + take] = chunk[:take]
                offset += take
                remaining -= take
                self._buffered_bytes = max(0, self._buffered_bytes - take)
                if take < len(chunk):
                    # Preserve exact PCM order for the next callback.
                    self._chunks.appendleft(chunk[take:])
                    break

            if offset > 0 and self._active_turn and self._started_for_turn != self._active_turn:
                self._started_for_turn = self._active_turn
                playback_turn = self._active_turn

        if playback_turn and self.on_playback_started:
            self.on_playback_started(playback_turn)

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd  # type: ignore

            device = resolve_device(self.output_device, input_device=False)
            self._stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=device,
                callback=self._callback,
                blocksize=max(1, self.sample_rate // 50),
            )
            self._stream.start()
            logger.info(
                "audio.player_started device=%s resolved_device=%s sample_rate=%d channels=%d",
                self.output_device,
                device,
                self.sample_rate,
                self.channels,
            )
        except Exception as exc:
            self._stream = None
            logger.exception("audio.player_start_failed")
            raise AudioPlayerError(f"扬声器播放启动失败：{exc}") from exc

    def enqueue(self, turn_id: str, pcm: bytes) -> None:
        if not pcm:
            return
        with self._queue_lock:
            if self._active_turn != turn_id:
                self._clear_unlocked()
                self._active_turn = turn_id
                self._started_for_turn = None
            self._chunks.append(bytes(pcm))
            self._buffered_bytes += len(pcm)
        logger.debug("audio.player_enqueue turn_id=%s bytes=%d buffered_ms=%d", turn_id, len(pcm), self.buffered_ms)

    def clear(self, turn_id: str | None = None) -> None:
        with self._queue_lock:
            if turn_id is not None and self._active_turn not in (None, turn_id):
                return
            self._clear_unlocked()
        logger.info("audio.player_cleared turn_id=%s", turn_id)

    def _clear_unlocked(self) -> None:
        self._chunks.clear()
        self._buffered_bytes = 0
        self._active_turn = None
        self._started_for_turn = None

    async def wait_until_empty(self, timeout_seconds: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self._buffered_bytes > 0 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)

    def stop(self) -> None:
        self.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        logger.info("audio.player_stopped")
