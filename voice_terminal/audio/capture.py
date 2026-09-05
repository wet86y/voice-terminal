from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .devices import resolve_device
from .ring_buffer import PcmRingBuffer
from ..controller.turn_workspace import AudioFrame

logger = logging.getLogger(__name__)


class AudioCaptureError(RuntimeError):
    pass


class AudioCapture:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        sample_rate: int,
        channels: int,
        block_ms: int,
        input_device: str | int | None,
        ring_buffer_ms: int,
    ) -> None:
        try:
            import sounddevice  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise AudioCaptureError("缺少 sounddevice，请先运行 scripts\\setup.ps1。") from exc
        self.loop = loop
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_ms = block_ms
        self.input_device = input_device
        self.queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=64)
        self._sample_position = 0
        self.ring_buffer = PcmRingBuffer(sample_rate * channels * 2 * ring_buffer_ms // 1000)
        self._stream: Any = None
        self._running = False
        self.last_error: str | None = None
        self._blocks_captured = 0
        self._bytes_captured = 0
        self._dropped_chunks = 0
        self._last_status = ""
        self._last_report_at = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def _on_audio(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status:
            self.last_error = str(status)
            if self.last_error != self._last_status:
                logger.warning("audio.capture_callback_status status=%s", self.last_error)
                self._last_status = self.last_error
        chunk = bytes(indata)
        frame = AudioFrame(chunk, self._sample_position, time.monotonic())
        self._sample_position += len(chunk) // 2
        self._blocks_captured += 1
        self._bytes_captured += len(chunk)
        self.ring_buffer.append(chunk)

        now = time.monotonic()
        if not self._last_report_at or now - self._last_report_at >= 1.0:
            logger.info(
                "audio.capture_progress blocks=%d bytes=%d queue=%d dropped=%d",
                self._blocks_captured,
                self._bytes_captured,
                self.queue.qsize(),
                self._dropped_chunks,
            )
            self._last_report_at = now

        def enqueue() -> None:
            if self.queue.full():
                self._dropped_chunks += 1
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

        self.loop.call_soon_threadsafe(enqueue)

    async def start(self) -> None:
        if self._running:
            return
        try:
            import sounddevice as sd  # type: ignore

            device = resolve_device(self.input_device, input_device=True)
            self.last_error = None
            self._blocks_captured = 0
            self._bytes_captured = 0
            self._dropped_chunks = 0
            self._last_status = ""
            self._last_report_at = 0.0
            logger.info(
                "audio.capture_start configured_device=%r resolved_device=%s sample_rate=%d channels=%d block_ms=%d",
                self.input_device,
                device,
                self.sample_rate,
                self.channels,
                self.block_ms,
            )
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=max(1, self.sample_rate * self.block_ms // 1000),
                device=device,
                callback=self._on_audio,
            )
            self._stream.start()
            self._running = True
            logger.info("audio.capture_started")
        except Exception as exc:
            self._stream = None
            logger.exception("audio.capture_start_failed")
            raise AudioCaptureError(f"麦克风采集启动失败：{exc}") from exc

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._running = False
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        logger.info(
            "audio.capture_stopped blocks=%d bytes=%d dropped=%d last_error=%s",
            self._blocks_captured,
            self._bytes_captured,
            self._dropped_chunks,
            self.last_error,
        )

    async def read(self) -> bytes:
        return (await self.queue.get()).pcm

    async def read_frame(self) -> AudioFrame:
        return await self.queue.get()

    def preroll(self, milliseconds: int) -> bytes:
        byte_count = self.sample_rate * self.channels * 2 * milliseconds // 1000
        return self.ring_buffer.read(byte_count)
