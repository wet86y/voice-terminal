"""TTS capabilities（语音合成能力）：未声明的后端维持原行为。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TtsCapabilities:
    acoustic_continuation: bool = False  # acoustic_continuation（跨段声学续接）。
    idle_prewarm: bool = False  # idle_prewarm（空闲后推理预热）。
    cancellation: bool = False  # cancellation（取消生成任务）。


def capabilities_of(client) -> TtsCapabilities:
    value = getattr(client, "capabilities", None)
    return value if isinstance(value, TtsCapabilities) else TtsCapabilities()
