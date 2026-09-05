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
        self._enqueued_bytes = 0
        self._played_bytes = 0
        self._allowed_key: str | None = None
        self._gated = False

    def allow(self, key: str) -> None:
        with self._queue_lock:
            self._clear_unlocked()
            self._gated = True
            self._allowed_key = key

    def block(self) -> bool:
        """Atomically close playback and report whether any audio already played."""
        with self._queue_lock:
            played = self._started_for_turn is not None
            self._gated = True
            self._allowed_key = None
            self._clear_unlocked()
            return played

    def has_started(self, key: str) -> bool:
        with self._queue_lock:
            return self._started_for_turn == key

    @property
    def buffered_ms(self) -> int:
        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            buffered_bytes = self._buffered_bytes
        return int(buffered_bytes * 1000 / bytes_per_second) if bytes_per_second else 0

    @property
    def enqueued_ms(self) -> int:
        """Total PCM accepted for the active playback key, in milliseconds."""

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            value = self._enqueued_bytes
        return int(value * 1000 / bytes_per_second) if bytes_per_second else 0

    @property
    def played_ms(self) -> int:
        """PCM consumed by the PortAudio callback for the active key."""

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            value = self._played_bytes
        return int(value * 1000 / bytes_per_second) if bytes_per_second else 0

    def progress(self, key: str | None = None) -> dict[str, int | bool | str | None]:
        """Return a thread-safe snapshot of the active playback queue.

        ``buffered_ms`` is the audio still held by the player.  ``played_ms``
        is based on bytes copied by the audio callback, which is the closest
        local signal to the Windows output device without adding another
        playback backend.  A key mismatch returns an empty snapshot so a
        previous turn cannot be mistaken for the current one.
        """

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            active = self._active_turn
            if key is not None and active != key:
                return {
                    "key": active,
                    "enqueued_ms": 0,
                    "played_ms": 0,
                    "buffered_ms": 0,
                    "started": False,
                }
            enqueued = self._enqueued_bytes
            played = self._played_bytes
            buffered = self._buffered_bytes
            started = self._started_for_turn == active and active is not None
        if not bytes_per_second:
            enqueued_ms = played_ms = buffered_ms = 0
        else:
            enqueued_ms = int(enqueued * 1000 / bytes_per_second)
            played_ms = int(played * 1000 / bytes_per_second)
            buffered_ms = int(buffered * 1000 / bytes_per_second)
        return {
            "key": active,
            "enqueued_ms": enqueued_ms,
            "played_ms": played_ms,
            "buffered_ms": buffered_ms,
            "started": started,
        }

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
                self._played_bytes += take
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
            if self._gated and turn_id != self._allowed_key:
                return
            if self._active_turn != turn_id:
                self._clear_unlocked()
                self._active_turn = turn_id
                self._started_for_turn = None
            self._chunks.append(bytes(pcm))
            self._buffered_bytes += len(pcm)
            self._enqueued_bytes += len(pcm)
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
        self._enqueued_bytes = 0
        self._played_bytes = 0
        self._active_turn = None
        self._started_for_turn = None

    async def wait_until_empty(self, timeout_seconds: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self.buffered_ms > 0 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if self.buffered_ms > 0:
            raise TimeoutError("PCM playback did not drain before its deadline")

    def stop(self) -> None:
        self.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        logger.info("audio.player_stopped")
