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
    route: str
    reason: str
    degraded: bool = False
    anti_spoofing: str = "unavailable"

    def to_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value
