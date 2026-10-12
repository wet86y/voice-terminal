from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    PCM（原始音频）不存入快照，embedding（向量）仅供内部准入流程使用。
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
    embedding: tuple[float, ...] = field(default_factory=tuple, repr=False) # 内部声纹证据，不进入前端。
    speech_ms: int = 0 # 本窗有效人声。
    new_speech_ms: int = 0 # 本次新增且不重复的人声时长。

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
    """增长式音频上下文；心跳由控制器负责，45毫秒即可开始分析。"""
    def __init__(self, embed_pcm, score_embedding, *, sample_rate=16000, buffer_ms=4000,
                 window_ms=3200, min_voiced_ms=45, min_decision_voiced_ms=45,
                 min_window_speech_ratio=0.0, confirm_threshold=0.60,
                 non_owner_threshold=0.35, max_snapshot_age_ms=800):
        if sample_rate != 16000 or window_ms < 45 or buffer_ms < window_ms:
            raise ValueError("声纹上下文需至少45毫秒且不超过缓冲")
        self.embed_pcm, self.score_embedding = embed_pcm, score_embedding
        self.sample_rate = sample_rate
        self.buffer_samples = sample_rate * buffer_ms // 1000
        self.window_samples = sample_rate * window_ms // 1000
        self.min_voiced_samples = sample_rate * 45 // 1000
        self.min_decision_voiced_samples = self.min_voiced_samples
        self.min_window_speech_ratio = 0.0
        self.confirm_threshold, self.non_owner_threshold = confirm_threshold, non_owner_threshold
        self.max_snapshot_age_ms = max_snapshot_age_ms
        self._chunks = deque()
        self._latest_sample = None
        self._inference_lock = asyncio.Lock()
        self._last_analyzed_end = -1
        self._last_snapshot = None

    @property
    def latest_sample(self):
        return self._latest_sample

    @property
    def buffered_samples(self):
        return sum(len(c.pcm)//2 for c in self._chunks)

    def clear(self):
        self._chunks.clear()
        self._latest_sample = None
        self._last_analyzed_end = -1
        self._last_snapshot = None

    def submit_frame(self, pcm, *, start_sample, speech=None):
        if not pcm or len(pcm)%2 or type(start_sample) is not int or start_sample < 0:
            return False
        end = start_sample + len(pcm)//2
        if self._latest_sample is not None:
            if end <= self._latest_sample: return False
            if start_sample > self._latest_sample: self.clear()
            elif start_sample < self._latest_sample:
                pcm = pcm[(self._latest_sample-start_sample)*2:]
                start_sample = self._latest_sample
        self._chunks.append(_PcmChunk(start_sample, bytes(pcm), speech))
        self._latest_sample = end
        cutoff = end-self.buffer_samples
        while self._chunks and self._chunks[0].end_sample <= cutoff:
            self._chunks.popleft()
        if self._chunks and self._chunks[0].start_sample < cutoff:
            first = self._chunks.popleft()
            self._chunks.appendleft(_PcmChunk(cutoff, first.pcm[(cutoff-first.start_sample)*2:], first.speech))
        return True

    async def analyze(self, *, candidate_start_sample=None, candidate_end_sample=None, completed_at=None):
        started = time.monotonic()
        end = self._latest_sample or 0
        if candidate_end_sample is not None: end=min(end,candidate_end_sample)
        start = max(0,end-self.window_samples,candidate_start_sample or 0,
                    self._chunks[0].start_sample if self._chunks else end)
        def unknown(reason,quality="unknown"):
            return RollingSpeakerSnapshot(start,end,candidate_start_sample,candidate_end_sample,
                started,time.monotonic() if completed_at is None else completed_at,
                quality,RollingSpeakerIdentity.UNKNOWN,None,reason)
        if (candidate_start_sample is None) != (candidate_end_sample is None):
            return unknown("incomplete_candidate_range")
        if end <= self._last_analyzed_end: return unknown("no_new_audio")
        if self._inference_lock.locked(): return unknown("inference_in_progress","busy")
        chunks=[c for c in self._chunks if c.start_sample<end and c.end_sample>start]
        if not chunks or any(c.speech is None for c in chunks): return unknown("vad_unavailable")
        voiced=sum(max(0,min(end,c.end_sample)-max(start,c.start_sample)) for c in chunks if c.speech)
        new_voiced_samples = sum(max(0, min(end,c.end_sample)-max(start,c.start_sample,self._last_analyzed_end)) for c in chunks if c.speech)
        new_voiced=any(c.speech and c.end_sample>self._last_analyzed_end for c in chunks)
        if not new_voiced or voiced < self.min_voiced_samples: return unknown("insufficient_voiced_audio","not_speech")
        pcm=b"".join(c.pcm[max(0,start-c.start_sample)*2:min(end-c.start_sample,len(c.pcm)//2)*2] for c in chunks)
        if len(pcm)<1440: return unknown("insufficient_audio","insufficient_audio")
        async with self._inference_lock:
            try:
                vector=tuple(await self.embed_pcm(pcm))
                score=self.score_embedding(vector)
                if score is not None and not math.isfinite(score): return unknown("invalid_score")
                identity=(RollingSpeakerIdentity.OWNER if score is not None and score>=self.confirm_threshold
                          else RollingSpeakerIdentity.UNKNOWN)
                snapshot=RollingSpeakerSnapshot(start,end,candidate_start_sample,candidate_end_sample,
                    started,time.monotonic() if completed_at is None else completed_at,"ready",identity,
                    score,"embedding_ready",vector,voiced*1000//self.sample_rate,new_voiced_samples*1000//self.sample_rate)
                self._last_analyzed_end=end
                self._last_snapshot=snapshot
                return snapshot
            except Exception as exc:
                if isinstance(exc, SpeakerWorkerError) and exc.code == "SPEAKER_AUDIO_QUALITY":
                    return unknown("audio_quality_rejected", exc.quality or "insufficient_audio")
                logger.warning("speaker.inference_failed（声纹推理失败） start_sample=%s end_sample=%s error=%s: %s", start, end, type(exc).__name__, str(exc)[:240])
                return unknown("inference_failed","unavailable")
