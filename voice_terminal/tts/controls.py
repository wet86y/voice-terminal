"""Speech controls（语音表现控制）：唯一能力登记、补丁校验与协议投影。"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from typing import Any


EMOTIONS = ("calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised")
CONTROL_NAMES = ("guidance", "speed", "volume")


class SpeechControlError(ValueError):
    """可选控制补丁无效；调用方保留旧值，不丢弃合法正文。"""


@dataclass(frozen=True)
class SpeechControls:
    guidance: str | None = None  # guidance（语气引导）；空值为模型自然表达。
    speed: float = 1.0  # speed（语速倍率）。
    volume: float = 1.0  # volume（相对音量倍率）。

    @property
    def is_default(self) -> bool:
        return self == DEFAULT_CONTROLS

    def summary(self) -> str:
        parts = []
        if self.guidance:
            text = " ".join(self.guidance.split())
            parts.append("语气=" + text[:32] + ("…" if len(text) > 32 else ""))
        if self.speed != 1:
            parts.append(f"语速={self.speed:g}")
        if self.volume != 1:
            parts.append(f"音量={self.volume:g}")
        return "；".join(parts)


DEFAULT_CONTROLS = SpeechControls()


@dataclass(frozen=True)
class SpeechControlProfile:
    model: str  # model（实际生效模型）。
    enabled: bool = True  # enabled（全局开关）。
    guidance_kind: str = "none"  # guidance_kind（引导形式）：枚举、描述或无。
    native_speed: bool = False  # native_speed（服务端原生变速）。
    native_volume: bool = False  # native_volume（服务端原生音量）。
    local_tempo: bool = False  # local_tempo（本机保音高处理器已就绪）。
    local_volume: bool = False  # local_volume（本机音量处理已接入）。
    voice: str = ""  # voice（音色身份），参与能力冻结。
    transport: str = ""  # transport（传输方式）。

    @property
    def names(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        return tuple(name for name, supported in (
            ("guidance", self.guidance_kind != "none"),
            ("speed", self.native_speed or self.local_tempo),
            ("volume", self.native_volume or self.local_volume),
        ) if supported)

    @property
    def identity(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()

    def normalize(self, values: SpeechControls) -> SpeechControls:
        """按当前实际能力消除旧后端或开关切换遗留的无效值。"""
        if not self.enabled:
            return DEFAULT_CONTROLS
        guidance = values.guidance if "guidance" in self.names else None
        if self.guidance_kind == "enum" and guidance not in EMOTIONS:
            guidance = None
        return SpeechControls(guidance, values.speed if "speed" in self.names else 1,
                              values.volume if "volume" in self.names else 1)

    def protocol(self) -> str:
        """只介绍当前可执行参数；未支持的名称及示例不进入当前能力说明。"""
        if not self.names:
            return "当前语音调节未启用或不可用，speak.controls（参数补丁列表）必须为 []。"
        lines = ["当前有效语音调节协议（替代此前的语音调节能力说明）："]
        if "guidance" in self.names:
            if self.guidance_kind == "enum":
                lines.append("guidance（语气引导）接受情绪枚举字符串：" + ", ".join(EMOTIONS)
                             + "；对应平静、高兴、悲伤、愤怒、害怕、厌恶、惊讶。")
            else:
                lines.append("guidance（语气引导）接受 style guidance（风格引导）的简洁自然语言描述，"
                             "最多500字，可描述情绪、语气和表达方式，例如‘轻柔、亲近，略带笑意’。"
                             "该描述是合成控制，不属于播报正文；当前参考音色提供声音身份。")
        if "speed" in self.names:
            lines.append("speed（语速）接受0.8至1.2的数值字符串，1为默认。长文本可根据内容适当提速，"
                         "不必每次调整。" + ("采用服务端原生处理。" if self.native_speed else "采用客户端保音高处理。"))
        if "volume" in self.names:
            lines.append("volume（相对音量）接受0.6至1.4的数值字符串，1为默认；无调整需求时不要改变音量。"
                         + ("采用服务端原生处理。" if self.native_volume else "采用客户端增益处理。"))
        lines.append("只输出需要改变的项；[]表示沿用当前队列参数，value（参数值）写default可恢复该项默认。"
                     "用户要求调整时，根据上下文维护后续对话的统一、连贯，避免无理由反复变化。"
                     "对话间是否再次调整由你判断；没有活动参数摘要时按默认理解。"
                     "描述中的节奏和响度不要与独立数值要求冲突。仅能使用本次列出的参数。")
        return "\n".join(lines)


def profile_for(client: Any, *, backend: str | None = None, enabled: bool = True,
                local_tempo: bool | None = None) -> SpeechControlProfile:
    """模型登记入口；新模型必须明确登记，未知模型不猜测能力。"""
    if hasattr(client, "current_client"):
        enabled = enabled and getattr(client, "speech_control_enabled", True)
        backend = getattr(client, "backend", backend)
        client = client.current_client()
    model = str(getattr(client, "local_model", None) or getattr(client, "model", ""))
    voice = str(getattr(client, "voice_id", ""))
    transport = str(getattr(client, "transport", ""))
    if model in {"speech-2.8-hd", "speech-2.8-turbo"} and backend != "voxcpm1":
        guidance = "none" if getattr(client, "emotion_status", "") == "unsupported" else "enum"
        return SpeechControlProfile(model, enabled, guidance, True, True, voice=voice, transport=transport)
    if model in {"voxcpm1", "voxcpm2"}:
        if local_tempo is None:
            from ..audio.speech_controls import tempo_available
            local_tempo = tempo_available()
        return SpeechControlProfile(model, enabled, "description" if model == "voxcpm2" else "none",
                                    local_tempo=local_tempo, local_volume=True, voice=voice, transport=transport)
    return SpeechControlProfile(model, enabled=False)


def merge_controls(profile: SpeechControlProfile, base: SpeechControls,
                   patch: Any) -> SpeechControls:
    """先完整校验再构造结果，任何无效项都不能部分提交。"""
    if not isinstance(patch, (list, tuple)) or len(patch) > 3:
        raise SpeechControlError("参数补丁必须是最多三项的列表。")
    changes: dict[str, Any] = {}
    for item in patch:
        if not isinstance(item, dict) or set(item) != {"name", "value"}:
            raise SpeechControlError("参数条目必须只有name（参数名）和value（参数值）。")
        name, value = item["name"], item["value"]
        if not isinstance(name, str) or name not in profile.names or name in changes:
            raise SpeechControlError("参数未启用、未登记或重复。")
        if not isinstance(value, str):
            raise SpeechControlError("参数值必须使用字符串编码。")
        value = value.strip()
        if value == "default":
            changes[name] = getattr(DEFAULT_CONTROLS, name)
        elif name == "guidance":
            if profile.guidance_kind == "enum" and value not in EMOTIONS:
                raise SpeechControlError("语气引导不属于当前允许的情绪枚举。")
            if not value or len(value) > 500 or any(ord(char) < 32 and not char.isspace() for char in value):
                raise SpeechControlError("风格引导必须是1至500字的有效描述。")
            changes[name] = " ".join(value.split())
        else:
            try:
                number = float(value)
            except ValueError as exc:
                raise SpeechControlError("语速或音量不是有效数值。") from exc
            low, high = (0.8, 1.2) if name == "speed" else (0.6, 1.4)
            if not math.isfinite(number) or not low <= number <= high:
                raise SpeechControlError(f"{name}（倍率）必须在{low}至{high}之间。")
            changes[name] = number
    return replace(profile.normalize(base), **changes)
