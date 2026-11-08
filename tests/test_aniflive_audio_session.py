"""AnifLive（本地语音）流式本机音量及参数切换。"""
import asyncio
from types import SimpleNamespace

import numpy as np

from voice_terminal.tts.aniflive_audio_session import AnifLiveAudioSession
from voice_terminal.tts.controls import SpeechControls


def test_audio_control_rollover_preserves_output_order_and_network_count():
    class Raw:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.turn_id = 'turn'
            self.voice_snapshot = None
            self.chunk_optimization = True
            self.first_audio_wait_ms = 0
            self.gpu_max_performance = False
            self.audio_bytes_received = 0
            self.completed_segments = 0
            self.server_finished = False
            self.first_audio_ready_at = self.first_audio_perf_at = 1.0
        async def start(self):
            pass
        async def send_text(self, text):
            data = np.array([1000, -1000], dtype='<i2').tobytes()
            self.audio_bytes_received += len(data)
            await self.queue.put(data)
        async def audio_chunks(self):
            while True:
                item = await self.queue.get()
                if item is None:
                    self.completed_segments = 1
                    self.server_finished = True
                    return
                yield item
        async def finish(self):
            await self.queue.put(None)
        async def close(self):
            pass
        async def cancel(self, **kwargs):
            return True
    async def run():
        client = SimpleNamespace(sample_rate=16000, session=lambda **kwargs: Raw())
        stream = AnifLiveAudioSession(client, Raw())
        values = SpeechControls(volume=1)
        stream.speech_control_reader = lambda: values
        await stream.start()
        async def collect():
            return b''.join([item async for item in stream.audio_chunks()])
        reader = asyncio.create_task(collect())
        await stream.send_text('第一句。')
        values = SpeechControls(volume=1.4)
        await stream.send_text('第二句。')
        await stream.finish()
        pcm = await reader
        assert np.frombuffer(pcm, dtype='<i2').tolist() == [1000, -1000, 1400, -1400]
        assert stream.received_bytes == len(pcm) == stream.audio_bytes_received
        assert stream.completed_segments == 2 and stream.server_finished
        assert [(s.input_start, s.input_end, s.output_start, s.output_end) for s in stream.audio_transform_spans] == [(0, 2, 0, 2), (2, 4, 2, 4)]
        await stream.close()
    asyncio.run(run())


def test_cancel_discards_processor_tail_and_closes_underlying_session():
    class Processor:
        def __init__(self, *args, **kwargs):
            self.closed = False
        def push(self, data):
            return b''
        def finish(self):
            raise AssertionError('取消时不能输出尾缓冲')
        def close(self):
            self.closed = True
    class Raw:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False
        async def start(self): pass
        async def send_text(self, text): await self.queue.put(b'\x01\x00')
        async def audio_chunks(self):
            while True: yield await self.queue.get()
        async def cancel(self, **kwargs): return True
        async def close(self): self.closed = True
    async def run():
        raw = Raw()
        stream = AnifLiveAudioSession(SimpleNamespace(sample_rate=16000), raw, processor_factory=Processor)
        await stream.start()
        await stream.send_text('取消前的文本。')
        await asyncio.sleep(0)
        processor = stream._processor
        assert await stream.cancel()
        assert processor.closed and raw.closed
        assert [item async for item in stream.audio_chunks()] == []
    asyncio.run(run())


def test_terminal_failure_is_not_reported_as_success():
    class Raw:
        async def start(self): pass
        async def send_text(self, text): pass
        async def finish(self): pass
        async def close(self): pass
        async def audio_chunks(self):
            raise RuntimeError('上游终态失败')
            yield b''
    async def run():
        import pytest
        stream = AnifLiveAudioSession(SimpleNamespace(sample_rate=16000), Raw())
        await stream.start()
        await stream.send_text('正文。')
        with pytest.raises(RuntimeError, match='上游终态失败'):
            await stream.finish()
        assert not stream.server_finished and not stream.generation_finished
        await stream.close()
    asyncio.run(run())
