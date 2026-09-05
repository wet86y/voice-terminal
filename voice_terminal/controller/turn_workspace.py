from __future__ import annotations

from dataclasses import dataclass, field
import time


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    start_sample: int
    captured_at: float

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // 2


class CurrentTurnAudioBuffer:
    """Bounded, sample-indexed PCM; rejects gaps rather than silently joining them."""

    def __init__(self, max_samples: int = 16000 * 62) -> None:
        self.max_samples = max_samples
        self.start_sample: int | None = None
        self.pcm = bytearray()

    @property
    def end_sample(self) -> int:
        return (self.start_sample or 0) + len(self.pcm) // 2

    def append(self, frame: AudioFrame) -> None:
        if len(frame.pcm) % 2:
            raise ValueError("PCM must contain complete int16 samples")
        if self.start_sample is None:
            self.start_sample = frame.start_sample
        if frame.start_sample > self.end_sample:
            raise ValueError("audio capture gap")
        overlap = max(0, self.end_sample - frame.start_sample)
        # Keep the newest bounded window and advance start_sample when the
        # caller supplies more contiguous PCM than the cap can hold.  Merely
        # truncating the append leaves end_sample behind the capture cursor
        # and makes the next valid frame look like a gap.
        self.pcm.extend(frame.pcm[overlap * 2:])
        excess = max(0, len(self.pcm) // 2 - self.max_samples)
        if excess:
            del self.pcm[:excess * 2]
            self.start_sample += excess

    def tail(self, seconds: int = 8) -> bytes:
        return bytes(self.pcm[-seconds * 16000 * 2:])


@dataclass
class TurnWorkspace:
    turn_id: str
    started_at: float = field(default_factory=time.monotonic)
    input_revision: int = 0
    generation_id: int = 0
    speech_version: int = 0
    committed: bool = False
    playback_started: bool = False
    hard_closed: bool = False
    raw_partial: str = ""
    paraformer_final: str = ""
    final_text: str = ""
    staged_response: str = ""
    audio_cues: list[dict] = field(default_factory=list)
    audio: CurrentTurnAudioBuffer = field(default_factory=CurrentTurnAudioBuffer)
    segments: list[dict] = field(default_factory=list)
    prediction: dict = field(default_factory=dict)
    prediction_revision: int | None = None
    tools: list[dict] = field(default_factory=list)
    agent_phase: str = "IDLE"
    analysis: dict = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def token(self) -> tuple[str, int]:
        return self.turn_id, self.generation_id

    @property
    def playback_key(self) -> str:
        return f"{self.turn_id}:{self.generation_id}"

    def freeze(self) -> tuple[str, int]:
        self.generation_id += 1
        self.committed = False
        self.agent_phase = "FINAL"
        return self.token

    def resume(self) -> None:
        self.generation_id += 1
        self.input_revision += 1
        self.speech_version += 1
        self.committed = False
        self.staged_response = ""
        self.final_text = ""
        self.audio_cues.clear()
        self.prediction.clear()
        self.prediction_revision = None
        self.agent_phase = "IDLE"

    def snapshot(self) -> dict:
        return {
            "input_revision": self.input_revision, "generation_id": self.generation_id,
            "committed": self.committed, "agent_phase": self.agent_phase,
            "paraformer_final": self.paraformer_final, "refined_text": self.final_text,
            "segments": list(self.segments), "prediction": dict(self.prediction),
            "prediction_revision": self.prediction_revision,
            "tools": list(self.tools), "audio_cues": list(self.audio_cues),
            "analysis": dict(self.analysis),
        }
