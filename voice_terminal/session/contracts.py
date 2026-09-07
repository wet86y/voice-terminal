from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class InputStatus(StrEnum):
    LISTENING = "LISTENING"
    FINALIZED = "FINALIZED"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    input_id: str | None = None
    episode_id: str | None = None
    decision_id: str | None = None
    speech_id: str | None = None

    def payload(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class InputTurn:
    input_id: str
    revision: int = 0
    partial_text: str = ""
    final_text: str = ""
    status: InputStatus = InputStatus.LISTENING
    audio_cues: list[dict[str, Any]] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)

    def update(self, text: str) -> None:
        self.revision += 1
        self.partial_text = text

    def finalize(self, text: str, *, audio_cues: list[dict[str, Any]] | None = None,
                 analysis: dict[str, Any] | None = None) -> None:
        self.revision += 1
        self.partial_text = ""
        self.final_text = text
        self.status = InputStatus.FINALIZED
        self.audio_cues = list(audio_cues or [])
        self.analysis = dict(analysis or {})

    def payload(self) -> dict[str, Any]:
        return {"input_id": self.input_id, "revision": self.revision, "partial_text": self.partial_text,
                "final_text": self.final_text, "status": self.status.value,
                "audio_cues": list(self.audio_cues), "analysis": dict(self.analysis)}


@dataclass(frozen=True)
class DeliveryReport:
    status: str
    termination: str
    heard_text: str = ""
    uncertain_text: str = ""
    unheard_text: str = ""
    precision: str = "unknown"

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, *, termination: str) -> "DeliveryReport":
        value = payload or {}
        return cls(status=str(value.get("status") or "none"), termination=str(value.get("termination") or termination),
                   heard_text=str(value.get("heard_text") or ""), uncertain_text=str(value.get("uncertain_text") or ""),
                   unheard_text=str(value.get("unheard_text") or ""), precision=str(value.get("precision") or "unknown"))


@dataclass(frozen=True)
class ToolExchange:
    name: str
    arguments: dict[str, Any]
    result: Any
    executed_at: str
    episode_id: str
    decision_id: str
    context_version: int
    source: str = "external_untrusted"

    def context_payload(self) -> dict[str, Any]:
        return {"source": self.source, "tool": self.name, "arguments": self.arguments,
                "executed_at": self.executed_at, "result": self.result}


@dataclass(frozen=True)
class SpeechOutput:
    speech_id: str
    episode_id: str
    decision_id: str
    input_id: str | None
    text: str
    status: str
    delivery: DeliveryReport
    kind: str = "formal"
