"""试听播放回归：首块直达、跨块重采样及远端结算沿用同一播放键。"""
import asyncio
import builtins
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.server.tts_preview_playback import TtsPreviewPlayback


def test_same_rate_mono_stream_never_loads_scientific_libraries(monkeypatch):
    imported = builtins.__import__
    def guard(name, *args, **kwargs):
        if name.split('.')[0] in {'scipy', 'soxr', 'numpy'}:
            raise AssertionError('同率单声道试听不应加载重采样或科学计算库。')
        return imported(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guard)

    async def scenario():
        events = []
        player = SimpleNamespace(sample_rate=32000, channels=1,
            allow_user_initiated=lambda key: events.append(('allow', key)),
            enqueue_async=AsyncMock(side_effect=lambda key, data: events.append(('audio', key, data))),
            finish_stream_async=AsyncMock(side_effect=lambda key: events.append(('finish', key))),
            wait_until_output_complete=AsyncMock(), has_started=lambda _: True)
        stream = TtsPreviewPlayback(player, 'preview-key', 32000)
        await stream.write(b'\x01\x00\x02')
        assert events == [('allow', 'preview-key'), ('audio', 'preview-key', b'\x01\x00')]
        await stream.write(b'\x00')
        await stream.finish()
        await stream.wait()
        assert events[-2:] == [('audio', 'preview-key', b'\x02\x00'), ('finish', 'preview-key')]
        assert stream.duration == 2 / 32000
    asyncio.run(scenario())


def test_cross_chunk_resampling_keeps_samples_and_duplicates_stereo_channels():
    import numpy as np
    import soxr
    async def scenario():
        chunks = []
        player = SimpleNamespace(sample_rate=48000, channels=2, allow=lambda _: None,
            enqueue=lambda key, data: chunks.append(data), finish_stream=lambda _: None)
        stream = TtsPreviewPlayback(player, 'stereo-key', 32000)
        source = (np.sin(np.arange(32000) * .02) * 10000).astype('<i2')
        raw = source.tobytes()
        for position in range(0, len(raw), 1023):
            await stream.write(raw[position:position + 1023])
        await stream.finish()
        output = np.frombuffer(b''.join(chunks), dtype='<i2').reshape(-1, 2)
        # 浮点参考避免两次独立整数抖动互相叠加；输出仍须在一个量化单位内。
        expected = np.rint(soxr.resample(source.astype('float32'), 32000, 48000, quality='HQ')).astype('int32')
        assert output.shape == (48000, 2)
        assert np.array_equal(output[:, 0], output[:, 1])
        assert np.max(np.abs(output[:, 0].astype('int32') - expected.astype('int32'))) <= 1
        assert stream.duration == 1
    asyncio.run(scenario())


def test_stream_rejects_partial_pcm_without_sending_end_marker():
    async def scenario():
        player = SimpleNamespace(sample_rate=32000, channels=1, finish_stream_async=AsyncMock())
        stream = TtsPreviewPlayback(player, 'bad-key', 32000)
        await stream.write(b'\x01')
        with pytest.raises(ValueError, match='不完整'):
            await stream.finish()
        player.finish_stream_async.assert_not_awaited()
    asyncio.run(scenario())
