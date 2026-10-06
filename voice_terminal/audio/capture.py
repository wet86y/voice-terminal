from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

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
        on_audio_frame: Callable[[bytes], None] | None = None,  # on_audio_frame（实际输入采样）。
    ) -> None:
        try:
            import sounddevice  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise AudioCaptureError("缺少 sounddevice，请先运行 scripts\\setup.ps1。") from exc
        self.loop = loop
        self.on_audio_frame = on_audio_frame
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
        self._last_drop_at: float | None = None
        self._last_queue_depth = 0
        self._last_status = ""
        self._last_report_at = 0.0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def diagnostics(self) -> dict[str, int | float | str | None]:
        """A small, thread-safe-enough capture health projection.

        The PortAudio callback is the only writer for these counters.  They
        are deliberately diagnostic data, not a second transport protocol:
        the sample indexes on :class:`AudioFrame` remain the authority used by
        the controller to reject a discontinuous turn.
        """

        return {
            "blocks": self._blocks_captured,
            "bytes": self._bytes_captured,
            "queue_depth": self._last_queue_depth,
            "dropped_chunks": self._dropped_chunks,
            "last_drop_at": self._last_drop_at,
            "last_error": self.last_error,
        }

    def _clear_pending_frames(self) -> None:
        """Drop frames captured by the session that has just ended.

        The PortAudio callback can enqueue a few frames while ``stop()`` is
        closing the stream.  Keeping them would make the next
        ``session.start`` consume audio from the previous session before the
        new microphone stream has produced a frame.
        """

        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _on_audio(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status:
            self.last_error = str(status)
            if self.last_error != self._last_status:
                logger.warning("audio.capture_callback_status status=%s", self.last_error)
                self._last_status = self.last_error
        chunk = bytes(indata)
        if self.on_audio_frame:
            self.on_audio_frame(chunk)
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
                self._last_drop_at = time.monotonic()
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                logger.warning(
                    "audio.capture_queue_overflow dropped=%d queue_capacity=%d next_start_sample=%d",
                    self._dropped_chunks,
                    self.queue.maxsize,
                    frame.start_sample,
                )
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # ``full`` can only race with another loop callback.  Keep
                # the loss visible; silently pretending this frame arrived
                # used to turn the controller's later continuity check into
                # an unexplained fatal session shutdown.
                self._dropped_chunks += 1
                self._last_drop_at = time.monotonic()
                logger.warning("audio.capture_queue_enqueue_failed dropped=%d", self._dropped_chunks)
            self._last_queue_depth = self.queue.qsize()

        self.loop.call_soon_threadsafe(enqueue)

    async def start(self) -> None:
        if self._running:
            return
        try:
            import sounddevice as sd  # type: ignore

            self._clear_pending_frames()
            device = resolve_device(self.input_device, input_device=True)
            self.last_error = None
            self._blocks_captured = 0
            self._bytes_captured = 0
            self._dropped_chunks = 0
            self._last_drop_at = None
            self._last_queue_depth = 0
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
        self._clear_pending_frames()
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
