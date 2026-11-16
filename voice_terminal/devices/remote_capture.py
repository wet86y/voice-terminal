"""Remote PCM capture port（远端原始音频采集接口）。"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable

from ..audio.ring_buffer import PcmRingBuffer
from ..controller.turn_workspace import AudioFrame
from .media_protocol import MediaFrame, MediaProtocolError
from .pcm_stream import PcmInputStream
from .registry import InteractionLease

logger = logging.getLogger(__name__)


class RemoteAudioCaptureError(RuntimeError):
    """远端音频采集接口状态错误。"""


class RemoteAudioCapture:
    """RemoteAudioCapture（远端采集端口）：复用核心控制器的六帧输入队列契约。

    The media handler owns framing, SSH authentication, and stream creation. This
    port only admits frames already bound to the current PcmInputStream lease.
    The queue drops its oldest frame on overflow so the controller observes the
    sample gap and performs its existing continuity recovery.
    """

    SAMPLE_RATE = 16_000  # SAMPLE_RATE（固定上行采样率，每秒采样数）。
    CHANNELS = 1  # CHANNELS（固定声道数）。
    FRAME_SAMPLES = 320  # FRAME_SAMPLES（20 毫秒帧的最大采样数）。
    QUEUE_FRAMES = 6  # QUEUE_FRAMES（远端待处理帧数上限）。

    def __init__(
        self,
        stream: PcmInputStream,
        *,
        ring_buffer_ms: int = 6_000,
        on_audio_frame: Callable[[bytes], None] | None = None,
    ) -> None:
        if not isinstance(stream, PcmInputStream):
            raise TypeError("远端采集端口必须绑定已验证的 PCM 输入流。")
        if type(ring_buffer_ms) is not int or not 20 <= ring_buffer_ms <= 60_000:
            raise ValueError("远端预录缓冲时长必须为 20 至 60000 毫秒。")
        if on_audio_frame is not None and not callable(on_audio_frame):
            raise TypeError("音频帧观察回调无效。")
        self._stream = stream
        self._on_audio_frame = on_audio_frame
        self.queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=self.QUEUE_FRAMES)
        self.ring_buffer = PcmRingBuffer(
            self.SAMPLE_RATE * self.CHANNELS * 2 * ring_buffer_ms // 1000
        )
        self._running = False
        self._started = False
        self._stopped = asyncio.Event()
        self._stopped.set()
        self._frames_received = 0
        self._bytes_received = 0
        self._dropped_chunks = 0
        self._last_drop_at: float | None = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        """running（是否接收帧）：只在流启动后接受当前代次帧。"""
        return self._running

    @property
    def device_id(self) -> str:
        """device_id（该采集流固定绑定的设备编号）。"""
        return self._stream.lease.device_id

    @property
    def lease(self) -> InteractionLease:
        """lease（该采集流固定绑定的交互租约）。"""
        return self._stream.lease

    @property
    def diagnostics(self) -> dict[str, int | float | str | None]:
        """diagnostics（有界诊断快照）：不包含音频内容、设备凭据或主机地址。"""
        return {
            "blocks": self._frames_received,
            "bytes": self._bytes_received,
            "queue_depth": self.queue.qsize(),
            "dropped_chunks": self._dropped_chunks,
            "last_drop_at": self._last_drop_at,
            "last_error": self._last_error,
            "clock_uncertainty_seconds": self._stream.clock_uncertainty_seconds,
        }

    async def start(self) -> None:
        """start（开始接收）：每条连续流只启动一次，旧队列不会跨流保留。"""
        if self._running:
            return
        if self._started or self._stream.closed:
            raise RemoteAudioCaptureError("远端 PCM 流已关闭，必须为新连接建立新流。")
        self._clear_pending_frames()
        self.ring_buffer.clear()
        self._frames_received = 0
        self._bytes_received = 0
        self._dropped_chunks = 0
        self._last_drop_at = None
        self._last_error = None
        self._stopped.clear()
        self._running = True
        self._started = True

    async def stop(self) -> None:
        """stop（停止接收）：关闭租约绑定的流并清空尚未处理的旧帧。"""
        self._running = False
        self._stream.close()
        self._clear_pending_frames()
        self._stopped.set()

    def accept_frame(self, frame: MediaFrame) -> AudioFrame | None:
        """accept_frame（接纳帧）：先核验核心租约与设备时钟，再入队。"""
        if not self._running:
            raise RemoteAudioCaptureError("远端音频采集尚未启动或已停止。")
        try:
            audio = self._stream.accept(frame)
            if audio is None:
                return None
            if (not audio.pcm or len(audio.pcm) % 2
                    or len(audio.pcm) // 2 > self.FRAME_SAMPLES):
                raise MediaProtocolError("远端音频采样长度不符合 16 kHz 单声道 PCM 帧。")
            if (isinstance(audio.captured_at, bool) or not isinstance(audio.captured_at, (int, float))
                    or not math.isfinite(audio.captured_at)):
                raise MediaProtocolError("远端音频采集时间无效。")
            self.ring_buffer.append(audio.pcm)
            self._frames_received += 1
            self._bytes_received += len(audio.pcm)
            if self._on_audio_frame is not None:
                try:
                    self._on_audio_frame(audio.pcm)
                except Exception as error:
                    # A meter or observer cannot invalidate authenticated PCM.
                    logger.warning("device.remote_audio_observer_failed error=%s", error)
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                    self._dropped_chunks += 1
                    self._last_drop_at = time.monotonic()
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(audio)
            return audio
        except Exception as error:
            self._last_error = str(error)[:300]
            self._running = False
            self._stream.close()
            self._clear_pending_frames()
            self.ring_buffer.clear()
            self._stopped.set()
            raise

    def _clear_pending_frames(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def read(self) -> bytes:
        """read（读取采样）：兼容仅读取 PCM 字节的旧采集调用面。"""
        return (await self.read_frame()).pcm

    async def read_frame(self) -> AudioFrame:
        """read_frame（读取采集帧）：保留采样位置与主机单调时钟映射。"""
        if not self._running and self.queue.empty():
            raise RemoteAudioCaptureError("远端音频流已停止。")
        frame_task = asyncio.create_task(self.queue.get())
        stopped_task = asyncio.create_task(self._stopped.wait())
        try:
            done, _ = await asyncio.wait(
                (frame_task, stopped_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped_task in done or not self._running:
                raise RemoteAudioCaptureError("远端音频流已停止。")
            frame = await frame_task
            if not self._running:
                raise RemoteAudioCaptureError("远端音频流已停止。")
            return frame
        finally:
            for task in (frame_task, stopped_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(frame_task, stopped_task, return_exceptions=True)

    def pending_frame_count(self) -> int:
        """pending_frame_count（待处理帧数）：与本机采集端口保持同一语义。"""
        return self.queue.qsize()

    def read_pending_frame(self) -> AudioFrame:
        """read_pending_frame（读取预录帧）：队列为空时抛出 asyncio.QueueEmpty。"""
        return self.queue.get_nowait()

    def preroll(self, milliseconds: int) -> bytes:
        """preroll（读取预录采样）：以 16 kHz 单声道 int16 计算字节数。"""
        if type(milliseconds) is not int or milliseconds < 0:
            raise ValueError("远端预录时长必须为非负整数。")
        byte_count = self.SAMPLE_RATE * self.CHANNELS * 2 * milliseconds // 1000
        return self.ring_buffer.read(byte_count)
