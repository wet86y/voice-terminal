from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .client import SpeakerWorkerError

logger = logging.getLogger(__name__)


class RollingSpeakerIdentity(StrEnum):
    OWNER = "owner"
    NON_OWNER = "non_owner"
    UNKNOWN = "unknown"


EmbedPcm = Callable[[bytes], Awaitable[tuple[float, ...]]]
ScoreEmbedding = Callable[[tuple[float, ...]], float | None]


@dataclass(frozen=True)
class RollingSpeakerSnapshot:
    """A single immutable decision for one absolute sample interval.

    ``analyzed_at`` and ``completed_at`` use the monotonic clock domain.
    The snapshot deliberately contains neither PCM nor the model embedding.
    """

    analysis_start_sample: int
    analysis_end_sample: int
    candidate_start_sample: int | None
    candidate_end_sample: int | None
    analyzed_at: float
    completed_at: float
    quality: str
    identity: RollingSpeakerIdentity
    score: float | None = None
    reason: str = "unknown"

    def is_fresh(self, *, now: float | None = None, max_age_ms: int = 800) -> bool:
        current = time.monotonic() if now is None else float(now)
        age_ms = (current - self.completed_at) * 1000.0
        return 0.0 <= age_ms <= max(0, int(max_age_ms))

    def covers_candidate(self, start_sample: int, end_sample: int) -> bool:
        if end_sample <= start_sample:
            return False
        return (
            start_sample <= self.analysis_start_sample
            and end_sample >= self.analysis_end_sample
            and self.candidate_start_sample == start_sample
            and self.candidate_end_sample == end_sample
        )


@dataclass(frozen=True)
class _PcmChunk:
    start_sample: int
    pcm: bytes
    speech: bool | None

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // 2


class RollingSpeakerSensor:
    """Bounded recent-audio sensor; inference is explicitly one-shot.

    The controller owns the cadence and calls :meth:`analyze` from its
    heartbeat callback. ``submit_frame`` is synchronous and copies only
    bounded 16-bit mono PCM into the rolling window.
    """

    def __init__(
        self,
        embed_pcm: EmbedPcm,
        score_embedding: ScoreEmbedding,
        *,
        sample_rate: int = 16000,
        buffer_ms: int = 4000,
        window_ms: int = 2000,
        min_voiced_ms: int = 700,
        min_decision_voiced_ms: int = 2000,
        min_window_speech_ratio: float = 0.40,
        confirm_threshold: float = 0.60,
        non_owner_threshold: float = 0.50,
        max_snapshot_age_ms: int = 800,
    ) -> None:
        if sample_rate < 1 or buffer_ms < 1 or window_ms < 1:
            raise ValueError("sample rate and rolling durations must be positive")
        if window_ms > buffer_ms:
            raise ValueError("analysis window must fit inside rolling buffer")
        if min_voiced_ms < 1 or min_voiced_ms > window_ms:
            raise ValueError("minimum voiced duration must fit inside analysis window")
        if min_decision_voiced_ms < min_voiced_ms:
            raise ValueError("decision speech duration must be at least the window gate")
        if not 0.0 <= min_window_speech_ratio <= 1.0:
            raise ValueError("speech ratio must be in [0, 1]")
        if not -1.0 <= non_owner_threshold <= confirm_threshold <= 1.0:
            raise ValueError("non-owner threshold must be below owner threshold")
        self.embed_pcm = embed_pcm
        self.score_embedding = score_embedding
        self.sample_rate = int(sample_rate)
        self.buffer_samples = int(sample_rate * buffer_ms / 1000)
        self.window_samples = int(sample_rate * window_ms / 1000)
        self.min_voiced_samples = int(sample_rate * min_voiced_ms / 1000)
        self.min_decision_voiced_samples = int(sample_rate * min_decision_voiced_ms / 1000)
        self.min_window_speech_ratio = float(min_window_speech_ratio)
        self.confirm_threshold = float(confirm_threshold)
        self.non_owner_threshold = float(non_owner_threshold)
        self.max_snapshot_age_ms = max(0, int(max_snapshot_age_ms))
        self._chunks: deque[_PcmChunk] = deque()
        self._latest_sample: int | None = None
        self._inference_lock = asyncio.Lock()
        self._last_owner_confirmed_at: float | None = None  # 最近一次达到主人基准阈值的时间。
        self._owner_continuity = False  # 当前有效判断是否处于连续主人窗口。
        self._last_confirmed_end_sample: int | None = None  # 已确认音频末端：重复分析不延长信任期限。

    @property
    def buffered_samples(self) -> int:
        if not self._chunks:
            return 0
        return self._chunks[-1].end_sample - self._chunks[0].start_sample

    @property
    def latest_sample(self) -> int | None:
        return self._latest_sample

    def clear(self) -> None:
        self._chunks.clear()
        self._latest_sample = None
        self._last_owner_confirmed_at = None
        self._owner_continuity = False
        self._last_confirmed_end_sample = None

    def submit_frame(self, pcm: bytes, *, start_sample: int,
                     speech: bool | None = None) -> bool:
        if not pcm or len(pcm) % 2 or isinstance(start_sample, bool) or start_sample < 0:
            return False
        frame = bytes(pcm)
        end_sample = start_sample + len(frame) // 2
        if self._latest_sample is not None:
            if end_sample <= self._latest_sample:
                return False
            if start_sample > self._latest_sample:
                # A discontinuity invalidates any window spanning the gap.
                self.clear()
            elif start_sample < self._latest_sample:
                # Ignore already-buffered overlap from duplicated callbacks.
                overlap_samples = self._latest_sample - start_sample
                frame = frame[overlap_samples * 2:]
                start_sample = self._latest_sample
                if not frame:
                    return False
        self._chunks.append(_PcmChunk(start_sample, frame, speech))
        self._latest_sample = start_sample + len(frame) // 2
        cutoff = self._latest_sample - self.buffer_samples
        while self._chunks and self._chunks[0].end_sample <= cutoff:
            self._chunks.popleft()
        if self._chunks and self._chunks[0].start_sample < cutoff:
            first = self._chunks.popleft()
            trim = cutoff - first.start_sample
            self._chunks.appendleft(_PcmChunk(cutoff, first.pcm[trim * 2:], first.speech))
        return True

    def _window(self, *, candidate_start_sample: int | None,
                candidate_end_sample: int | None) -> tuple[bytes, int, int, int, bool] | None:
        if not self._chunks or self._latest_sample is None:
            return None
        end = self._latest_sample if candidate_end_sample is None else int(candidate_end_sample)
        start_limit = None if candidate_start_sample is None else int(candidate_start_sample)
        start = end - self.window_samples
        if start_limit is not None and start < start_limit:
            return None
        oldest = self._chunks[0].start_sample
        if end > self._latest_sample or start < oldest:
            return None
        pieces: list[bytes] = []
        voiced = 0
        cursor = start
        for chunk in self._chunks:
            left = max(start, chunk.start_sample)
            right = min(end, chunk.end_sample)
            if right <= left:
                continue
            if left != cursor:
                return None
            offset = (left - chunk.start_sample) * 2
            length = (right - left) * 2
            pieces.append(chunk.pcm[offset:offset + length])
            if chunk.speech is True:
                voiced += right - left
            cursor = right
        if cursor != end:
            return None
        return b"".join(pieces), start, end, voiced, all(
            chunk.speech is not None for chunk in self._chunks
            if chunk.start_sample < end and chunk.end_sample > start
        )

    def _decision_evidence(self, *, candidate_start_sample: int | None,
                           candidate_end_sample: int | None) -> tuple[int, bool]:
        """Count distinct voiced samples across the bounded candidate history.

        Model input remains a fixed recent ``window_samples`` slice. Identity
        authorization uses the wider rolling history so natural pauses do not
        make a 2 s effective-speech requirement impossible within a 2.5 s
        spoken candidate.
        """
        if not self._chunks or self._latest_sample is None:
            return 0, False
        start = self._chunks[0].start_sample
        end = self._latest_sample
        if candidate_start_sample is not None:
            start = max(start, int(candidate_start_sample))
        if candidate_end_sample is not None:
            end = min(end, int(candidate_end_sample))
        if end <= start:
            return 0, False
        voiced = 0
        covered = start
        for chunk in self._chunks:
            left = max(start, chunk.start_sample)
            right = min(end, chunk.end_sample)
            if right <= left:
                continue
            if left != covered:
                return 0, False
            if chunk.speech is None:
                return voiced, False
            if chunk.speech:
                voiced += right - left
            covered = right
        return voiced, covered == end

    @staticmethod
    def _unknown(*, start: int, end: int, candidate_start: int | None,
                 candidate_end: int | None, analyzed_at: float, reason: str,
                 quality: str = "unknown", completed_at: float | None = None) -> RollingSpeakerSnapshot:
        return RollingSpeakerSnapshot(
            start, end, candidate_start, candidate_end, analyzed_at,
            time.monotonic() if completed_at is None else completed_at,
            quality, RollingSpeakerIdentity.NON_OWNER, None, reason,
        )

    async def analyze(self, *, candidate_start_sample: int | None = None,
                      candidate_end_sample: int | None = None,
                      completed_at: float | None = None) -> RollingSpeakerSnapshot:
        analyzed_at = time.monotonic()
        # Candidate endpoints are both supplied or both omitted. A partial
        # bound cannot establish safe association with the current utterance.
        if (candidate_start_sample is None) != (candidate_end_sample is None):
            return self._unknown(start=0, end=0, candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="incomplete_candidate_range")
        selected = self._window(candidate_start_sample=candidate_start_sample,
                                candidate_end_sample=candidate_end_sample)
        if selected is None:
            latest = self._latest_sample or 0
            return self._unknown(start=max(0, latest - self.window_samples), end=latest,
                                 candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="window_unavailable", quality="insufficient_window")
        pcm, start, end, window_voiced_samples, window_vad_measured = selected
        voiced_samples, vad_measured = self._decision_evidence(
            candidate_start_sample=candidate_start_sample,
            candidate_end_sample=candidate_end_sample,
        )
        if not window_vad_measured or not vad_measured:
            return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="vad_unavailable", quality="unknown")
        if self._inference_lock.locked():
            return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="inference_in_progress", quality="busy")
        if (
            window_voiced_samples < self.min_voiced_samples
            or window_voiced_samples / max(1, self.window_samples) < self.min_window_speech_ratio
        ):
            return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="insufficient_voiced_audio", quality="not_speech")
        # A valid model window is not sufficient identity evidence. Preserve
        # the existing distinct-speech authorization floor separately so a
        # short/noisy candidate cannot trigger a veto or owner-only barge-in.
        if vad_measured and voiced_samples < self.min_decision_voiced_samples:
            return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                 candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                 reason="insufficient_decision_speech", quality="insufficient_audio")
        async with self._inference_lock:
            try:
                embedding = await self.embed_pcm(pcm)
                score = self.score_embedding(tuple(float(value) for value in embedding))
                if score is None or not math.isfinite(float(score)):
                    return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                         candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                         reason="profile_unavailable", quality="ready")
                score = max(-1.0, min(1.0, float(score)))
                completed = time.monotonic() if completed_at is None else float(completed_at)
                threshold = self.confirm_threshold  # threshold（本窗实际主人阈值）。
                if self._last_owner_confirmed_at is not None:
                    limit = 10.0 if self._owner_continuity else 5.0
                    if 0 <= completed - self._last_owner_confirmed_at <= limit:
                        threshold = self.non_owner_threshold
                identity = RollingSpeakerIdentity.OWNER if score >= threshold else RollingSpeakerIdentity.NON_OWNER
                self._owner_continuity = identity == RollingSpeakerIdentity.OWNER and score < self.confirm_threshold
                if score >= self.confirm_threshold:
                    if end != self._last_confirmed_end_sample:
                        self._last_owner_confirmed_at = completed
                        self._last_confirmed_end_sample = end
                elif identity == RollingSpeakerIdentity.NON_OWNER:
                    self._last_owner_confirmed_at = None
                return RollingSpeakerSnapshot(
                    start, end, candidate_start_sample, candidate_end_sample,
                    analyzed_at, completed, "ready", identity, score,
                    "owner_confirmed" if identity == RollingSpeakerIdentity.OWNER
                    else "non_owner_confirmed" if identity == RollingSpeakerIdentity.NON_OWNER
                    else "non_owner_confirmed",
                )
            except Exception as exc:
                if isinstance(exc, SpeakerWorkerError) and exc.code == "SPEAKER_AUDIO_QUALITY":
                    quality = exc.quality or "insufficient_audio"
                    logger.info("speaker.rolling_audio_rejected start_sample=%s end_sample=%s quality=%s",
                                start, end, quality)
                    return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                         candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                         reason="audio_quality_rejected", quality=quality)
                logger.warning("speaker.rolling_inference_failed start_sample=%s end_sample=%s code=%s error=%s: %s",
                               start, end, getattr(exc, "code", "INFERENCE_ERROR"), type(exc).__name__, str(exc)[:300],
                               exc_info=True)
                return self._unknown(start=start, end=end, candidate_start=candidate_start_sample,
                                     candidate_end=candidate_end_sample, analyzed_at=analyzed_at,
                                     reason="inference_failed", quality="unavailable")
