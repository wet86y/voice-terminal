from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioDeviceInfo:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float


def query_devices() -> list[AudioDeviceInfo]:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:
        raise AudioDeviceError("缺少 sounddevice，请先运行 scripts\\setup.ps1。") from exc

    result: list[AudioDeviceInfo] = []
    for index, raw in enumerate(sd.query_devices()):
        item: dict[str, Any] = dict(raw)
        result.append(
            AudioDeviceInfo(
                index=index,
                name=str(item.get("name", "")),
                max_input_channels=int(item.get("max_input_channels", 0)),
                max_output_channels=int(item.get("max_output_channels", 0)),
                default_samplerate=float(item.get("default_samplerate", 0)),
            )
        )
    return result


def resolve_device(value: str | int | None, *, input_device: bool) -> int | None:
    if value is None or value == "":
        return None
    devices = query_devices()
    if isinstance(value, int):
        if not 0 <= value < len(devices):
            raise AudioDeviceError(f"音频设备索引不存在：{value}")
        return value
    needle = value.casefold()
    matches = [
        device
        for device in devices
        if needle in device.name.casefold()
        and ((device.max_input_channels > 0) if input_device else (device.max_output_channels > 0))
    ]
    if not matches:
        direction = "输入" if input_device else "输出"
        raise AudioDeviceError(f"找不到匹配的音频{direction}设备：{value}")
    return matches[0].index
