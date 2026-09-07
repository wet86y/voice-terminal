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
    epoch: int = 0
    phase: str = "CAPTURING"
    # Episode ownership is captured when this audio turn first enters the
    # agent.  Cleanup must use this stable value even if the runtime has
    # already ended the Episode.
    episode_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
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
    tools: list[dict] = field(default_factory=list)
    agent_phase: str = "IDLE"
    analysis: dict = field(default_factory=dict)
    # TTS delivery bookkeeping is kept separate from the generated response.
    # A response can be fully generated while only a prefix reached the user.
    tts_segments: list[dict] = field(default_factory=list)
    delivery: dict = field(default_factory=dict)
    interruption_recorded: bool = False
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def token(self) -> tuple[str, int]:
        return self.turn_id, self.epoch

    @property
    def playback_key(self) -> str:
        return f"{self.turn_id}:{self.epoch}"

    def freeze(self) -> tuple[str, int]:
        self.committed = False
        self.phase = "FINALIZING"
        self.agent_phase = "FINAL"
        return self.token

    def resume(self, epoch: int) -> None:
        self.epoch = epoch
        self.phase = "CAPTURING"
        self.committed = False
        self.staged_response = ""
        self.final_text = ""
        self.audio_cues.clear()
        self.agent_phase = "IDLE"
        self.tts_segments.clear()
        self.delivery.clear()
        self.interruption_recorded = False

    def snapshot(self) -> dict:
        return {
            "conversation_id": self.episode_id, "turn_id": self.turn_id, "epoch": self.epoch, "phase": self.phase,
            "committed": self.committed, "agent_phase": self.agent_phase,
            "paraformer_final": self.paraformer_final, "refined_text": self.final_text,
            "segments": list(self.segments),
            "tools": list(self.tools), "audio_cues": list(self.audio_cues),
            "analysis": dict(self.analysis),
            "tts_segments": [dict(item) for item in self.tts_segments],
            "delivery": dict(self.delivery),
        }
