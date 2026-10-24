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
        self._tail_snapshot: tuple[int, bytes] | None = None
        from ..resources import registry, ResourcePolicy
        self._reuse_snapshots = ResourcePolicy.load().shared_audio
        registry.observe(self, "turn_audio", lambda value: len(value.pcm) +
                         (len(value._tail_snapshot[1]) if value._tail_snapshot else 0))

    @property
    def end_sample(self) -> int:
        return (self.start_sample or 0) + len(self.pcm) // 2

    def append(self, frame: AudioFrame) -> None:
        self._tail_snapshot = None
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
        if not self._reuse_snapshots:
            return bytes(self.pcm[-seconds * 16000 * 2:])
        if self._tail_snapshot is None or self._tail_snapshot[0] != seconds:
            self._tail_snapshot = (seconds, bytes(self.pcm[-seconds * 16000 * 2:]))
        return self._tail_snapshot[1]


@dataclass
class TurnWorkspace:
    turn_id: str
    # Explicit cancellation/output generation.  This changes only when a
    # turn is explicitly stopped or resumed; it is never used to judge ASR,
    # refinement, or tool data as semantically old.
    generation: int = 0
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
    # The fast Paraformer final is the first-class commit.  SenseVoice may
    # arrive later and is retained separately as supplemental evidence.
    refined_text: str = ""
    refinement_available: bool = False
    transcript_updates: list[dict] = field(default_factory=list)
    final_text: str = ""
    raw_final_text: str = ""  # raw_final_text（最终稿热词替换前原文）。
    raw_refined_text: str = ""  # raw_refined_text（精修稿热词替换前原文）。
    hotword_final: dict = field(default_factory=dict)  # hotword_final（最终稿替换及耗时记录）。
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
    tts_prewarm_started: bool = False
    tts_prewarm_status: str = "NOT_REQUESTED"
    speaker_decision: dict = field(default_factory=dict)
    speaker_observations: list[dict] = field(default_factory=list)
    speaker_state: str = "unknown"
    speaker_degraded_reason: str = ""
    speaker_blocked: bool = False
    speaker_routing_finalized: bool = False  # speaker_routing_finalized（本段声纹路由已结算）。
    admission_phase: str = ""  # 本轮准入阶段，非拦截模式为空。
    speaker_summary: dict = field(default_factory=dict)  # 与原话分开的声纹语义摘要。
    published_transcripts: dict[str, tuple] = field(default_factory=dict)  # 已发布正文及来源，用于显示事件去重。
    published_partial_text: str = ""  # 最近已展示的原始部分转写，避免声纹心跳覆盖热词修订或重复刷新。
    # Capture the routing mode at turn start.  Product mode changes can then
    # take effect on the next speech segment without rewriting a turn that is
    # already being finalized.
    speaker_mode: str = "off"

    @property
    def token(self) -> tuple[str, int]:
        return self.turn_id, self.generation

    @property
    def playback_key(self) -> str:
        return f"{self.turn_id}:{self.generation}"

    def freeze(self) -> tuple[str, int]:
        self.committed = False
        self.phase = "FINALIZING"
        self.agent_phase = "FINAL"
        return self.token

    def resume(self) -> None:
        self.generation += 1
        self.phase = "CAPTURING"
        self.committed = False
        self.staged_response = ""
        self.final_text = ""
        self.raw_final_text = ""
        self.hotword_final.clear()
        self.paraformer_final = ""
        self.audio_cues.clear()
        self.agent_phase = "IDLE"
        self.tts_segments.clear()
        self.delivery.clear()
        self.interruption_recorded = False
        self.speaker_decision.clear()
        self.speaker_observations.clear()
        self.speaker_state = "unknown"
        self.speaker_degraded_reason = ""
        self.speaker_blocked = False
        self.speaker_routing_finalized = False
        self._speaker_first_final_requested = False  # 同轮恢复讲话，撤销此前闭段的末次检查要求。
        self.published_transcripts.clear()
        self.published_partial_text = ""

    @property
    def transcript_visible(self) -> bool:
        """原文保留在工作区；未通过声纹准入的正文不进入界面快照。"""
        if self.speaker_mode != "filter":
            return True
        if self.speaker_summary:
            return bool(self.speaker_summary.get("admitted"))
        return (self.speaker_decision.get("route") == "main"
                or (self.speaker_routing_finalized and not self.speaker_blocked))

    def snapshot(self) -> dict:
        visible = self.transcript_visible
        return {
            "conversation_id": self.episode_id, "turn_id": self.turn_id, "output_generation": self.generation,
            "phase": self.phase,
            "committed": self.committed, "agent_phase": self.agent_phase,
            "paraformer_final": self.paraformer_final if visible else "",
            "refined_text": self.refined_text if visible else "",
            "admission_phase": self.admission_phase, "speaker_summary": dict(self.speaker_summary),
            "refinement_available": self.refinement_available if visible else False,
            "transcript_updates": [dict(item) for item in self.transcript_updates] if visible else [],
            "tts_prewarm_started": self.tts_prewarm_started,
            "tts_prewarm_status": self.tts_prewarm_status,
            "speaker_decision": dict(self.speaker_decision),
            "speaker_observations": [dict(item) for item in self.speaker_observations[-8:]],
            "speaker_state": self.speaker_state,
            "speaker_degraded_reason": self.speaker_degraded_reason,
            "speaker_blocked": self.speaker_blocked,
            "speaker_mode": self.speaker_mode,
            "segments": list(self.segments) if visible else [],
            "tools": list(self.tools), "audio_cues": list(self.audio_cues),
            "analysis": dict(self.analysis),
            "tts_segments": [dict(item) for item in self.tts_segments],
            "delivery": dict(self.delivery),
        }
