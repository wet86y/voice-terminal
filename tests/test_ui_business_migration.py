"""本轮界面业务接入回归；合成采样仅用于单元验证，不代表现场采集验收。"""
import asyncio
import math
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from voice_terminal.audio.levels import AudioLevelMeter, pcm_levels
from voice_terminal.core.state import RuntimeState, TerminalState
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.paths import AppPaths
from voice_terminal.server.product import ProductSettingsError, ProductSettingsStore
from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
from voice_terminal.speaker.enrollment_capture import CaptureControl, EnrollmentCaptureError, capture_segment


def tone(frequency=1000, rate=16000, duration=0.1):
    return b"".join(struct.pack('<h', round(8000 * math.sin(2 * math.pi * frequency * i / rate)))
                    for i in range(round(rate * duration)))


def test_source_checkout_reuses_formal_data_without_relaxing_program_overlap(tmp_path):
    manifest = tmp_path / 'apps/desktop/Cargo.toml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[workspace]', encoding='utf-8')
    install = tmp_path / '.runtime/delivery/VoiceTerminal'
    data = install / 'data'
    with patch.dict(os.environ, {'VOICE_PROGRAM_ROOT': str(tmp_path), 'VOICE_INSTALL_ROOT': str(install)}):
        assert AppPaths.resolve(data_dir=data).data_root == data.resolve()
        with pytest.raises(ValueError): AppPaths.resolve(data_dir=tmp_path / 'config/data')


def test_levels_measure_pcm_bands_and_real_silence():
    assert pcm_levels(bytes(3200), 16000) == [0] * 5
    for index, frequency in enumerate((100, 400, 800, 1600, 3000)):
        values = pcm_levels(tone(frequency), 16000)
        assert values[index] > 0.5
        assert all(0 <= value <= 1 for value in values)
        assert sum(value > 0.1 for value in values) == 1


def test_audio_generation_expiry_and_interruption_clear_levels():
    async def run():
        now, events = [0.0], []
        async def publish(value):
            events.append(value)
        state = RuntimeState()
        meter = AudioLevelMeter(state, SimpleNamespace(publish=publish), clock=lambda: now[0])
        state.set_state(TerminalState.LISTENING, listening=True)
        meter.feed('input', tone(), 16000)
        await meter.sample()
        assert any(events[-1].payload['levels'])
        now[0] = 0.301
        await meter.sample()
        assert events[-1].payload['levels'] == [0] * 5
        state.set_state(TerminalState.SPEAKING)
        meter.feed('playback', tone(), 16000)
        await meter.sample()
        assert events[-1].payload['source'] == 'playback'
        assert any(events[-1].payload['levels'])
        state.set_state(TerminalState.INTERRUPTING)
        await meter.sample()
        assert events[-1].payload['levels'] == [0] * 5
        state.set_state(TerminalState.SPEAKING)
        await meter.sample()
        assert events[-1].payload['levels'] == [0] * 5, '上一轮的播放帧不能被重新使用'
        assert events[-1].payload['generation'] == state.snapshot()['audio_generation']
        meter.feed('input', bytes(16000 * 2), 16000)
        assert len(meter.frames['input'][-1]) <= 6400, '采样回调只保留有界帧'
    asyncio.run(run())


def test_actual_output_drives_levels_while_input_is_finalizing():
    async def run():
        events, playing = [], [True]
        async def publish(value): events.append(value)
        state = RuntimeState()
        state.set_state(TerminalState.FINALIZING, listening=True)
        meter = AudioLevelMeter(state, SimpleNamespace(publish=publish), playback_active=lambda: playing[0])
        meter.feed('playback', tone(), 16000)
        await meter.sample()
        assert events[-1].payload['source'] == 'playback'
        assert any(events[-1].payload['levels']), '正式输入状态与真实播放状态独立'
        playing[0] = False
        await meter.sample()
        assert events[-1].payload['levels'] == [0] * 5
    asyncio.run(run())


@pytest.mark.parametrize('seconds', [5, 180, 240, 600, 1800])
def test_silence_range_persists_and_failed_save_recovers(tmp_path: Path, seconds):
    async def run():
        config = AppConfig(persona=PersonaConfig(directory=tmp_path / 'persona'))
        path = tmp_path / 'settings.json'
        store = ProductSettingsStore(path=path, config=config)
        await store.save_silence({'seconds': seconds})
        assert ProductSettingsStore(path=path, config=config).snapshot()['silence']['seconds'] == seconds
        assert config.agent.final_silence_ms != seconds * 1000, '保存不提前改变当前会话配置'
        with patch.object(store, '_save_settings', side_effect=OSError('写入失败')):
            with pytest.raises(OSError):
                await store.save_silence({'seconds': 12})
        assert store.snapshot()['silence']['seconds'] == seconds
        for invalid in (4, 1801, 5.1, True):
            with pytest.raises(ProductSettingsError):
                await store.save_silence({'seconds': invalid})
    asyncio.run(run())


def test_capture_stops_own_stream_and_cancel_discards_pcm():
    control = CaptureControl()
    closed = []
    class Stream:
        def __init__(self, **options): self.callback = options['callback']
        def __enter__(self):
            self.callback(tone(duration=5.1), 81600, None, None)
            control.stop.set()
            return self
        def __exit__(self, *_): closed.append(True)
    with patch('sounddevice.RawInputStream', Stream), patch('voice_terminal.speaker.enrollment_capture.resolve_device', return_value=None):
        captured = capture_segment(seconds=8, control=control)
        assert captured.duration_ms == 5100
        assert closed and control.closed.is_set()
        control.cancel.set()
        with pytest.raises(EnrollmentCaptureError, match='取消'):
            capture_segment(seconds=8, control=control)


def test_capture_cancel_and_late_response_cannot_restore_enrollment():
    async def run():
        runtime = SpeakerRuntimeMixin()
        runtime.state = RuntimeState()
        runtime.config = AppConfig()
        runtime.audio_processor = None
        runtime._speaker_enrollment = {'enroll': {'segments': {}}}
        entered = asyncio.Event()
        def capture(**kwargs):
            loop.call_soon_threadsafe(entered.set)
            while not kwargs['control'].cancel.wait(0.01): pass
            kwargs['control'].closed.set()
            # 模拟硬件已经返回但响应较迟，运行层仍须拒绝。
            return SimpleNamespace(duration_ms=8000)
        loop = asyncio.get_running_loop()
        async def start():
            async with runtime._capture_speaker_pcm(owner='enroll', seconds=8, capture_id='capture'):
                pytest.fail('取消的采集不能进入质量检查')
        with patch('voice_terminal.server.speaker_runtime.capture_segment', capture):
            task = asyncio.create_task(start())
            await entered.wait()
            with pytest.raises(ValueError, match='5 秒'):
                await runtime.stop_speaker_capture('enroll', 'capture')
            assert (await runtime.stop_speaker_capture('enroll', 'capture', cancel=True))['stopped']
            runtime.cancel_speaker_enrollment('enroll')
            with pytest.raises(EnrollmentCaptureError, match='取消'): await task
            assert runtime._speaker_captures == {}
    asyncio.run(run())
