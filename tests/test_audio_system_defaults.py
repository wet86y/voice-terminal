"""System defaults（系统默认音频设备）：迁移后不再匹配旧机器的名称或编号。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from voice_terminal.audio.capture import AudioCapture
from voice_terminal.audio import devices as audio_devices
from voice_terminal.audio.devices import AudioDeviceError, resolve_device
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
    # Windows（操作系统）的系统声音映射器编号由底层枚举解析，不是实体声卡编号。
    monkeypatch.setattr(audio_devices, "_windows_output_mapper", lambda: 37)
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
    assert all(options["device"] == (37 if kind == "output" and audio_devices.sys.platform == "win32" else None)
               for kind, options in opened)
    assert len(closed) == 3
    assert local.read_text("utf-8") == original


@pytest.mark.parametrize("name", ["Microsoft 声音映射器 - Output", "Microsoft Sound Mapper - Output"])
@pytest.mark.parametrize("value", [None, ""])
def test_windows_output_uses_mapper_instead_of_cached_physical_default(monkeypatch, name, value):
    import sounddevice as sd

    monkeypatch.setattr(audio_devices.sys, "platform", "win32")
    # 枚举顺序、接口编号和映射器编号均不固定；默认输出刻意指向旧实体设备。
    hostapis = [
        {"name": "Windows WASAPI", "devices": [9], "default_output_device": 9},
        {"name": "MME", "devices": [4, 28, 6, 7], "default_output_device": 6},
    ]
    devices = {
        9: {"name": "另一个接口 - Output", "max_output_channels": 2},
        4: {"name": "Microsoft 声音映射器 - Input", "max_output_channels": 0},
        28: {"name": name, "max_output_channels": 2},
        6: {"name": "启动时的实体扬声器", "max_output_channels": 2},
        7: {"name": "切换后的实体扬声器", "max_output_channels": 2},
    }
    monkeypatch.setattr(sd, "query_hostapis", lambda: hostapis)
    monkeypatch.setattr(sd, "query_devices", lambda index: devices[index])
    assert resolve_device(value, input_device=False) == 28
    # Windows（操作系统）切换实体默认设备后仍选同一系统路由端点。
    hostapis[1]["default_output_device"] = 7
    assert resolve_device(value, input_device=False) == 28


def test_windows_missing_mapper_does_not_silently_pin_physical_output(monkeypatch):
    import sounddevice as sd

    monkeypatch.setattr(audio_devices.sys, "platform", "win32")
    monkeypatch.setattr(sd, "query_hostapis", lambda: [
        {"name": "MME", "devices": [2], "default_output_device": 2},
    ])
    monkeypatch.setattr(sd, "query_devices", lambda index: {
        "name": "实体扬声器", "max_output_channels": 2,
    })
    with pytest.raises(AudioDeviceError, match="系统声音映射器"):
        resolve_device(None, input_device=False)


@pytest.mark.parametrize("platform,input_device", [("linux", False), ("darwin", False), ("win32", True)])
def test_other_default_paths_keep_backend_selection(monkeypatch, platform, input_device):
    monkeypatch.setattr(audio_devices.sys, "platform", platform)
    monkeypatch.setattr(audio_devices, "_windows_output_mapper", lambda: pytest.fail("不应解析输出映射器"))
    assert resolve_device(None, input_device=input_device) is None


def test_explicit_device_selection_still_resolves_normally(monkeypatch):
    monkeypatch.setattr(audio_devices.sys, "platform", "win32")
    monkeypatch.setattr(audio_devices, "query_devices", lambda: [
        audio_devices.AudioDeviceInfo(0, "测试扬声器", 0, 2, 48000),
    ])
    assert resolve_device(0, input_device=False) == 0
    assert resolve_device("测试扬声器", input_device=False) == 0
