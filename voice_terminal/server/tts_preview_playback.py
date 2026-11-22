"""试听流式播放：同率单声道直接入队，异率跨块保留重采样状态。"""
from __future__ import annotations


class TtsPreviewPlayback:
    """TtsPreviewPlayback（试听播放流）：沿用正式播放器和固定远端路由。"""

    def __init__(self, player, key: str, source_rate: int, *, on_first_enqueue=None):
        self.player = player  # player（正式程序播放器）。
        self.key = key  # key（本次任务固定播放键）。
        self.source_rate = source_rate  # source_rate（输入音频采样率）。
        self.input_bytes = 0  # input_bytes（已接收的完整单声道采样字节数）。
        self.started = False  # started（已允许本任务主动播放）。
        self.pending = b''  # pending（跨块不完整的 PCM16 样本）。
        self.resampler = None  # resampler（有状态采样率转换器，仅异率时加载）。
        self.on_first_enqueue = on_first_enqueue  # on_first_enqueue（首块入队耗时记录）。
        self.enqueued = False  # enqueued（已经送入有效输出采样）。
        self.completed = False  # completed（设备已确认本次输出完成）。
        if source_rate != int(player.sample_rate):
            import soxr
            self.resampler = soxr.ResampleStream(source_rate, int(player.sample_rate), 1, dtype='int16', quality='HQ')

    @property
    def duration(self):
        return self.input_bytes / (self.source_rate * 2)

    async def write(self, pcm: bytes):
        raw = self.pending + pcm
        size = len(raw) // 2 * 2
        self.pending = raw[size:]
        raw = raw[:size]
        self.input_bytes += size
        if not raw:
            return
        if self.resampler is not None:
            import numpy as np
            raw = self.resampler.resample_chunk(np.frombuffer(raw, dtype='<i2')).astype('<i2', copy=False).tobytes()
        await self._enqueue(raw)

    async def _enqueue(self, pcm):
        if not pcm:
            return
        if int(self.player.channels) == 2:
            import numpy as np
            pcm = np.repeat(np.frombuffer(pcm, dtype='<i2').reshape(-1, 1), 2, axis=1).astype('<i2', copy=False).tobytes()
        if not self.started:
            allow = getattr(self.player, 'allow_user_initiated', None)
            (allow if callable(allow) else self.player.allow)(self.key)
            self.started = True
        enqueue = getattr(self.player, 'enqueue_async', None)
        if callable(enqueue):
            await enqueue(self.key, pcm)
        else:
            self.player.enqueue(self.key, pcm)
        if not self.enqueued:
            self.enqueued = True
            if self.on_first_enqueue:
                self.on_first_enqueue()

    async def finish(self):
        if self.pending:
            raise ValueError('试听音频包含不完整的 PCM16（16 位采样）数据。')
        if self.resampler is not None:
            import numpy as np
            tail = self.resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True)
            await self._enqueue(tail.astype('<i2', copy=False).tobytes())
        if not self.started:
            return
        finish = getattr(self.player, 'finish_stream_async', None)
        if callable(finish):
            await finish(self.key)
        else:
            finish = getattr(self.player, 'finish_stream', None)
            if callable(finish):
                finish(self.key)

    async def wait(self):
        await self.player.wait_until_output_complete(timeout_seconds=max(10.0, self.duration * 2 + 3.0))
        if not self.player.has_started(self.key):
            raise RuntimeError('试听音频未从当前输出设备播放，请检查静音策略或设备状态。')
        self.completed = True
