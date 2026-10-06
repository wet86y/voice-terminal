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
    audio_generation: int = 0  # audio_generation（音频轮代）：状态变化后拒绝旧音量帧。
    wake_always_on: bool = False
    input_owner: str = "none"
    agent: dict[str, Any] = field(default_factory=dict)
    services: dict[str, str] = field(
        default_factory=lambda: {
            "audio": "OFFLINE",
            "kws": "OFFLINE",
            "asr": "OFFLINE",
            "speaker": "OFFLINE",
            "llm": "OFFLINE",
            "tts": "OFFLINE",
            "web_search": "OFFLINE",
            "shadow_agent": "OFFLINE",
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
            "input_runtime": True,
            # Runtime capabilities are kept in the snapshot so a reconnecting
            # client can explain why protected barge-in is unavailable and
            # which TTS scheduling path is active.
            "audio_processing_status": "DISABLED",
            "audio_processing_error": "",
            "tts_transport": "websocket",
            "tts_scheduling": "auto",
            "delivery": {},
            "refinement_available": False,
            "tts_prewarm_started": False,
            "tts_prewarm_status": "NOT_REQUESTED",
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
                "asr_final_to_commit_ms": None,
                "refine_duration_ms": None,
                "refine_to_redecision_ms": None,
                "agent_request_to_speak_ms": None,
                "tts_prewarm_requested_ms": None,
                "tts_prewarm_ready_ms": None,
                "tts_checkout_ms": None,
                "tts_cold_start_ms": None,
                "speech_end_to_first_pcm_ms": None,
                "first_pcm_to_player_submit_ms": None,
                "replacement_count": 0,
                "turn_total_ms": None,
                "first_segment_ms": None,
                "smart_turn_ms": None,
                "sensevoice_refine_ms": None,
                "commit_to_final_llm_ms": None,
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
                or (name == "kws" and self.wake_always_on
                    and self.services.get(name) == "ERROR")
                or (name == "asr" and self.wake_always_on
                    and self.services.get(name) == "ERROR")
                for name in required
            )
        )

    def set_service(self, name: str, status: str) -> None:
        if name not in self.services:
            raise KeyError(f"unknown service: {name}")
        self.services[name] = status

    def set_state(self, state: TerminalState, *, listening: bool | None = None) -> None:
        if state != self.state or (listening is not None and listening != self.listening):
            self.audio_generation += 1
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
        for key in ("turn_id", "output_generation", "phase", "committed", "agent_phase", "paraformer_final", "refined_text", "refinement_available", "segments", "tools", "audio_cues", "analysis", "tts_segments", "speaker_decision"):
            self.current.pop(key, None)
        self.current.update(tts_prewarm_started=False, tts_prewarm_status="NOT_REQUESTED")
        self.latency = {
            "complete": False,
            "values": {key: None for key in self.latency["values"]},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent": dict(self.agent),
            "state": self.state.value,
            "listening": self.listening,
            "audio_generation": self.audio_generation,
            "can_start": self.can_start,
            "wake_always_on": self.wake_always_on,
            "input_owner": self.input_owner,
            "services": dict(self.services),
            "current": dict(self.current),
            "latency": {
                "complete": self.latency["complete"],
                "values": dict(self.latency["values"]),
            },
        }
