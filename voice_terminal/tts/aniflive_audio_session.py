"""AnifLive（本地语音）的流式本机变速与音量事务。"""
from __future__ import annotations

import asyncio

from ..audio.speech_controls import PcmControlProcessor
from .controls import DEFAULT_CONTROLS


_END = object()


class AnifLiveAudioSession:
    """将多个相同参数的原生会话串成一笔有序播放。"""
    bidi = False
    reusable = False
    managed_controls = True

    def __init__(self, client, session, *, processor_factory=PcmControlProcessor):
        self.client = client
        self._raw = session
        self._first_raw = session
        self._processor_factory = processor_factory
        self._processor = None
        self._values = None
        self._reader = None
        self._queue = asyncio.Queue(maxsize=3)
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._done = False
        self._error = None
        self._received_bytes = 0
        self._network_bytes = 0
        self._completed = 0
        self.audio_transform_spans = []  # audio_transform_spans（源与输出音频的累计帧边界）。
        self._input_frames = 0
        self._output_frames = 0
        self.speech_control_reader = None

    def __getattr__(self, name):
        return getattr(self._raw, name)

    @property
    def chunk_optimization(self):
        return self._raw.chunk_optimization

    @chunk_optimization.setter
    def chunk_optimization(self, value):
        self._raw.chunk_optimization = value

    @property
    def first_audio_wait_ms(self):
        return self._raw.first_audio_wait_ms

    @first_audio_wait_ms.setter
    def first_audio_wait_ms(self, value):
        self._raw.first_audio_wait_ms = value

    @property
    def gpu_max_performance(self):
        return self._raw.gpu_max_performance

    @gpu_max_performance.setter
    def gpu_max_performance(self, value):
        self._raw.gpu_max_performance = value

    @property
    def first_audio_ready_at(self):
        return self._first_raw.first_audio_ready_at

    @property
    def first_audio_perf_at(self):
        return self._first_raw.first_audio_perf_at

    @property
    def received_bytes(self):
        return self._received_bytes

    @property
    def audio_bytes_received(self):
        return self._network_bytes + self._raw.audio_bytes_received

    @property
    def completed_segments(self):
        return self._completed + self._raw.completed_segments

    @property
    def generation_finished(self):
        return self._done and self._error is None

    @property
    def server_finished(self):
        return self._done and self._error is None and self._raw.server_finished

    @property
    def voice_snapshot(self):
        return self._raw.voice_snapshot

    async def start(self):
        if self._reader is not None:
            return
        self._raw.speech_control_reader = lambda: self.speech_control_reader() if callable(self.speech_control_reader) else DEFAULT_CONTROLS
        await self._raw.start()
        self._reader = asyncio.create_task(self._pump(self._raw), name='aniflive-audio-controls')

    async def _pump(self, raw):
        try:
            async for data in raw.audio_chunks():
                if self._processor is None:
                    raise RuntimeError('本地音频处理器尚未初始化。')
                output = await asyncio.to_thread(self._processor.push, data)
                if output:
                    self._received_bytes += len(output)
                    await self._queue.put(output)
            if self._processor is not None:
                tail = await asyncio.to_thread(self._processor.finish)
                if tail:
                    self._received_bytes += len(tail)
                    await self._queue.put(tail)
                span = self._processor.span(input_start=self._input_frames, output_start=self._output_frames)
                self.audio_transform_spans.append(span)
                self._input_frames, self._output_frames = span.input_end, span.output_end
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = error
            await self._queue.put(error)

    async def send_text(self, text):
        async with self._send_lock:
            if self._closed or self._done:
                raise RuntimeError('本地语音会话已经结束。')
            controls = self.speech_control_reader() if callable(self.speech_control_reader) else DEFAULT_CONTROLS
            values = (controls.speed, controls.volume)
            if self._values is not None and values != self._values:
                await self._raw.finish()
                if self._reader:
                    await self._reader
                if self._error:
                    raise self._error
                old = self._raw
                following = self.client.session(turn_id=old.turn_id, voice_snapshot=old.voice_snapshot)
                following.chunk_optimization = old.chunk_optimization
                following.first_audio_wait_ms = 0
                following.gpu_max_performance = old.gpu_max_performance
                self._network_bytes += old.audio_bytes_received
                self._completed += old.completed_segments
                await old.close()
                self._raw = following
                self._reader = None
                self._processor = None
                await self.start()
            if self._values is None or values != self._values:
                self._processor = self._processor_factory(self.client.sample_rate, speed=values[0], volume=values[1])
                self._values = values
            await self._raw.send_text(text)

    async def finish(self):
        if self._done or self._closed:
            return
        await self._raw.finish()
        if self._reader:
            await self._reader
        if self._error:
            raise self._error
        self._done = True
        await self._queue.put(_END)

    async def audio_chunks(self):
        while not self._closed:
            item = await self._queue.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def cancel(self, **kwargs):
        self._closed = True
        if self._reader and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._processor is not None:
            self._processor.close()
        try:
            return await self._raw.cancel(**kwargs)
        finally:
            await self._raw.close()
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(_END)

    async def close(self):
        self._closed = True
        if self._reader and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._processor is not None:
            self._processor.close()
        await self._raw.close()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(_END)
