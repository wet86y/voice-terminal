from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SpeakerMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    FILTER = "filter"


class SpeakerState(StrEnum):
    UNKNOWN = "unknown"
    OWNER_CONFIRMED = "owner_confirmed"
    OWNER_CONTINUITY = "owner_continuity"
    NON_OWNER_CONFIRMED = "non_owner_confirmed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SpeakerObservation:
    turn_id: str
    generation: int
    window_started_sample: int
    window_ended_sample: int
    similarity: float | None = None
    matched_template_id: str | None = None
    speech_ms: int = 0
    quality: str = "unknown"
    model_version: str = ""
    timestamp: str = ""
    embedding: tuple[float, ...] = field(default_factory=tuple, repr=False)
    #: Fraction of the window that the upstream VAD marked as speech.  ``None``
    #: means the caller could not supply a VAD verdict and the worker fell back
    #: to its energy heuristic.
    speech_ratio: float | None = None
    #: turn_speech_ms（整轮不重复人声时长，毫秒）：从本轮开始到当前观察窗末尾，
    #: 按 VAD 区间并集累计；不同于 speech_ms（单窗人声时长），不会重复计入重叠窗。
    turn_speech_ms: int | None = None

    def to_payload(self, *, include_embedding: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value["embedding"] = list(self.embedding) if include_embedding else None
        return value


@dataclass(frozen=True)
class SpeakerDecision:
    turn_id: str
    generation: int
    user_id: str | None
    state: SpeakerState
    similarity: float | None
    threshold_used: float | None
    speech_ms: int
    observation_count: int
    continuity_source: str | None
    route: str  # route（准入路由）：main（主输入）、blocked（拦截）或 pending（待判）。
    reason: str
    degraded: bool = False
    anti_spoofing: str = "unavailable"
    #: Number of windows that contributed to the aggregated turn embedding.
    aggregated_windows: int = 0

    def to_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value
