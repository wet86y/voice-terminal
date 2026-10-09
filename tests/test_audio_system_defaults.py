"""System defaults（系统默认音频设备）：迁移后不再匹配旧机器的名称或编号。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from voice_terminal.audio.capture import AudioCapture
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.config import load_config
from voice_terminal.speaker.enrollment_capture import capture_segment


@pytest.mark.parametrize("filename", ["default.yaml", "local.yaml.example"])
def test_shipped_audio_templates_do_not_pin_hardware(filename):
    config = yaml.safe_load((Path(__file__).parents[1] / "config" / filename).read_text("utf-8"))
    assert config["audio"]["input_device"] is None
    assert config["audio"]["output_device"] is None


@pytest.mark.parametrize("devices", [
    ("原电脑的麦克风阵列", "原电脑的扬声器"),
    (123, 456),
])
def test_legacy_config_uses_system_devices_for_listening_playback_and_enrollment(tmp_path, monkeypatch, devices):
    program = tmp_path / "程序"
    config_dir = program / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "default.yaml").write_text(
        (Path(__file__).parents[1] / "config/default.yaml").read_text("utf-8"), encoding="utf-8",
    )
    data = tmp_path / "用户 数据"
    local = data / "config/local.yaml"
    local.parent.mkdir(parents=True)
    original = yaml.safe_dump({"audio": {
        "input_device": devices[0], "output_device": devices[1], "block_ms": 30,
    }}, allow_unicode=True)
    local.write_text(original, encoding="utf-8")
    monkeypatch.setenv("VOICE_PROGRAM_ROOT", str(program))
    monkeypatch.setenv("VOICE_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("VOICE_DATA_DIR", str(data))
    config = load_config(config_dir / "default.yaml", local)
    assert config.audio.input_device is None and config.audio.output_device is None
    assert config.audio.block_ms == 30

    opened = []
    closed = []

    class Stream:
        def __init__(self, kind, options):
            opened.append((kind, options))
            self.options = options

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            closed.append(self)

        def __enter__(self):
            # 声纹录制的流送回一段完整采样；测试不访问物理麦克风。
            frames = 5 * self.options["samplerate"]
            self.options["callback"](b"\x01\x00" * frames, frames, None, None)
            return self

        def __exit__(self, *_args):
            self.close()

    def no_hardware_matching():
        raise AssertionError("系统默认模式不应枚举并匹配旧设备")

    monkeypatch.setattr("voice_terminal.audio.devices.query_devices", no_hardware_matching)
    monkeypatch.setattr("sounddevice.RawInputStream", lambda **options: Stream("input", options))
    monkeypatch.setattr("sounddevice.RawOutputStream", lambda **options: Stream("output", options))

    async def listen():
        capture = AudioCapture(loop=asyncio.get_running_loop(), sample_rate=config.audio.sample_rate,
                               channels=config.audio.channels, block_ms=config.audio.block_ms,
                               input_device=config.audio.input_device, ring_buffer_ms=config.audio.ring_buffer_ms)
        await capture.start()
        assert capture.running
        await capture.stop()

    asyncio.run(listen())
    player = PcmPlayer(sample_rate=config.tts.sample_rate, channels=1, output_device=config.audio.output_device)
    player.start()
    player.stop()
    recorded = capture_segment(seconds=5, input_device=config.audio.input_device)
    assert recorded.duration_ms == 5000
    assert [kind for kind, _options in opened] == ["input", "output", "input"]
    assert all(options["device"] is None for _kind, options in opened)
    assert len(closed) == 3
    assert local.read_text("utf-8") == original
