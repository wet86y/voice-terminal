from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TerminalState(StrEnum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    FINALIZING = "FINALIZING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    ERROR = "ERROR"


SERVICE_READY_STATES = {"READY", "RUNNING", "STREAMING", "CONNECTED"}


@dataclass
class RuntimeState:
    state: TerminalState = TerminalState.STARTING
    listening: bool = False
    agent: dict[str, Any] = field(default_factory=dict)
    services: dict[str, str] = field(
        default_factory=lambda: {
            "audio": "OFFLINE",
            "kws": "OFFLINE",
            "asr": "OFFLINE",
            "llm": "OFFLINE",
            "tts": "OFFLINE",
            "web_search": "OFFLINE",
        }
    )
    current: dict[str, Any] = field(
        default_factory=lambda: {
            "partial_text": "",
            "final_text": "",
            "assistant_text": "",
            "tts_text": "",
            "tts_status": "IDLE",
            "audio_buffer_ms": 0,
            "tts_returned_ms": 0,
            "tts_played_ms": 0,
            "tts_output_estimated_ms": 0,
            "tts_remaining_ms": 0,
            "turn_id": None,
            "v02": False,
            # Runtime capabilities are kept in the snapshot so a reconnecting
            # client can explain why protected barge-in is unavailable and
            # which TTS scheduling path is active.
            "audio_processing_status": "DISABLED",
            "audio_processing_error": "",
            "tts_transport": "websocket",
            "tts_scheduling": "auto",
            "delivery": {},
        }
    )
    latency: dict[str, Any] = field(
        default_factory=lambda: {
            "complete": False,
            "values": {
                "wake_detection_ms": None,
                "asr_first_text_ms": None,
                "speech_end_confirmation_ms": None,
                "llm_first_token_ms": None,
                "llm_request_to_first_tts_text_ms": None,
                "tts_text_to_first_audio_ms": None,
                "first_audio_to_playback_ms": None,
                "speech_end_to_playback_ms": None,
                "turn_total_ms": None,
            },
        }
    )

    @property
    def can_start(self) -> bool:
        required = ("audio", "kws", "asr", "llm", "tts")
        return (
            not self.listening
            and self.state != TerminalState.ERROR
            and all(
                self.services.get(name) in SERVICE_READY_STATES
                or (name == "kws" and self.services.get(name) == "DISABLED")
                for name in required
            )
        )

    def set_service(self, name: str, status: str) -> None:
        if name not in self.services:
            raise KeyError(f"unknown service: {name}")
        self.services[name] = status

    def set_state(self, state: TerminalState, *, listening: bool | None = None) -> None:
        self.state = state
        if listening is not None:
            self.listening = listening

    def clear_current(self) -> None:
        self.current.update(
            partial_text="",
            final_text="",
            assistant_text="",
            tts_text="",
            tts_status="IDLE",
            audio_buffer_ms=0,
            tts_returned_ms=0,
            tts_played_ms=0,
            tts_output_estimated_ms=0,
            tts_remaining_ms=0,
            delivery={},
        )
        for key in ("input_revision", "generation_id", "committed", "agent_phase", "paraformer_final", "refined_text", "segments", "prediction", "tools", "audio_cues", "analysis", "tts_segments"):
            self.current.pop(key, None)
        self.latency = {
            "complete": False,
            "values": {key: None for key in self.latency["values"]},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent": dict(self.agent),
            "state": self.state.value,
            "listening": self.listening,
            "can_start": self.can_start,
            "services": dict(self.services),
            "current": dict(self.current),
            "latency": {
                "complete": self.latency["complete"],
                "values": dict(self.latency["values"]),
            },
        }
