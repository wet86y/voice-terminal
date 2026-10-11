from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .contracts import SpeakerDecision, SpeakerObservation, SpeakerState

try:  # numpy is already a runtime dependency of the audio stack.
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SpeakerStateConfig:
    confirm_threshold: float = 0.60
    continuity_threshold: float = 0.50
    min_speech_ms: int = 700
    min_turn_speech_ms: int = 1500
    continuity_window_ms: int = 5000
    continuity_max_ms: int = 10000

    def __post_init__(self) -> None:
        if not 0.0 <= self.continuity_threshold <= self.confirm_threshold <= 1.0:
            raise ValueError("continuity_threshold must be below confirm_threshold in [0, 1]")
        if self.min_speech_ms < 1:
            raise ValueError("speaker window speech duration must be positive")
        if self.min_turn_speech_ms < self.min_speech_ms:
            raise ValueError("min_turn_speech_ms must be at least min_speech_ms")
        if self.continuity_window_ms < 1 or self.continuity_max_ms < self.continuity_window_ms:
            raise ValueError("speaker continuity windows are invalid")


@dataclass(frozen=True)
class TurnAggregate:
    """单轮聚合结果：窗口分数按语音加权，整轮语音时长不重复计数。"""

    embedding: tuple[float, ...] | None
    score: float
    total_speech_ms: int
    window_count: int


class SpeakerStateMachine:
    """Turn-level speaker decision.

    Every eligible window of a turn is folded into a single speech-weighted
    aggregate before it is compared with a threshold.  Scoring individual
    500 ms windows against a fixed cut-off (the previous design) made the
    outcome depend on how many windows happened to clear the line, and its
    hysteresis confirmed the owner from a single window while requiring two
    consecutive windows to reject one.  Aggregating first removes both
    asymmetries and is far less sensitive to one noisy window.
    """

    def __init__(self, *, config: SpeakerStateConfig | None = None,
                 user_id: str | None = "owner",
                 scorer: Any = None) -> None:
        self.config = config or SpeakerStateConfig()
        self.user_id = user_id
        # Optional callable ``(TurnAggregate) -> float | None`` supplied by the
        # router so that the authoritative turn score can be computed from the
        # aggregated embedding against the enrolled centroid.  Without it the
        # machine falls back to the speech-weighted mean of window scores.
        self.scorer = scorer
        self.reset()

    def _turn_score(self, aggregate: TurnAggregate) -> float:
        if self.scorer is not None:
            try:
                resolved = self.scorer(aggregate)
            except Exception:  # pragma: no cover - defensive, never block routing
                resolved = None
            if resolved is not None and math.isfinite(float(resolved)):
                return max(-1.0, min(1.0, float(resolved)))
        return aggregate.score

    def reset(self, *, turn_id: str | None = None, generation: int = 0) -> None:
        self.turn_id = turn_id or ""
        self.generation = generation
        self.state = SpeakerState.NON_OWNER_CONFIRMED
        self.observations: list[SpeakerObservation] = []
        self._unavailable_reason: str | None = None
        self._aggregate: TurnAggregate | None = None
        # ``_last_owner_confirmed_at`` deliberately survives a turn boundary:
        # OWNER_CONTINUITY exists to keep a multi-turn conversation alive after
        # a real confirmation, bounded by continuity_window_ms (entry) and
        # continuity_max_ms (renewal).  Clearing it here would make that state
        # unreachable.
        if not isinstance(getattr(self, "_last_owner_confirmed_at", None), float):
            self._last_owner_confirmed_at: float | None = None
        self._last_confirmed_evidence: tuple[str, int, int] | None = None  # 已确认的音频证据标识。
        self._evidence_sequence = 0  # 合格身份证据序号，不受观察列表裁剪影响。

    def clear_continuity(self) -> None:
        """Drop session-scoped owner trust (session reset / profile change)."""
        self._last_owner_confirmed_at = None
        self._last_confirmed_evidence = None

    def mark_unavailable(self, reason: str = "speaker_unavailable") -> None:
        self.state = SpeakerState.UNAVAILABLE
        self._unavailable_reason = str(reason or "speaker_unavailable")[:240]
        self.clear_continuity()

    # ------------------------------------------------------------------ input
    def _accept_observation(self, observation: SpeakerObservation) -> bool:
        if observation.turn_id != self.turn_id or observation.generation != self.generation:
            return False
        self.observations.append(observation)
        if self._is_eligible(observation):
            self._evidence_sequence += 1
        if len(self.observations) > 64:
            self.observations.pop(0)
        return True

    @staticmethod
    def _speech_ms(observation: SpeakerObservation) -> int:
        try:
            return max(0, int(observation.speech_ms or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _is_eligible(self, observation: SpeakerObservation) -> bool:
        """ready（质量就绪）、有效分数且有足够人声时，才构成新的身份证据。"""
        if not isinstance(observation.quality, str) or observation.quality.strip().lower() != "ready":
            return False
        try:
            return (observation.similarity is not None
                    and math.isfinite(float(observation.similarity))
                    and self._speech_ms(observation) >= self.config.min_speech_ms)
        except (TypeError, ValueError, OverflowError):
            return False

    def _eligible(self) -> list[SpeakerObservation]:
        """Windows that may carry identity evidence.

        Only a window the worker explicitly marked ``ready`` counts; a missing
        or unknown quality marker cannot authorise owner privileges.
        """
        result: list[SpeakerObservation] = []
        for observation in self.observations:
            if self._is_eligible(observation):
                result.append(observation)
        return result

    def _build_aggregate(self, eligible: list[SpeakerObservation]) -> TurnAggregate | None:
        if not eligible:
            return None
        # The worker's cumulative turn duration uses the union of VAD frames,
        # so adjacent windows cannot count the same audio several times. Keep
        # the score-normalization weight separate from this decision gate.
        turn_speech_values = [
            max(0, int(item.turn_speech_ms))
            for item in self.observations
            if item.turn_speech_ms is not None
        ]
        total_speech = (
            max(turn_speech_values) if turn_speech_values
            else sum(self._speech_ms(item) for item in eligible)
        )
        window_weight_total = sum(self._speech_ms(item) for item in eligible)
        weight_total = float(window_weight_total) if window_weight_total > 0 else float(len(eligible))
        weighted_score = 0.0
        weighted_vectors: list[Any] = []
        vector_weight = 0.0
        dimension: int | None = None
        for item in eligible:
            weight = float(self._speech_ms(item)) if window_weight_total > 0 else 1.0
            weighted_score += max(-1.0, min(1.0, float(item.similarity))) * weight
            if np is None or not item.embedding:
                continue
            vector = np.asarray(item.embedding, dtype=np.float32).reshape(-1)
            if not vector.size or not np.isfinite(vector).all():
                continue
            if dimension is None:
                dimension = int(vector.size)
            if int(vector.size) != dimension:
                # Mixed dimensions mean the profile changed mid-turn; keep the
                # score-level fallback instead of mixing incompatible vectors.
                weighted_vectors = []
                vector_weight = 0.0
                dimension = None
                break
            norm = float(np.linalg.norm(vector))
            if norm <= 1e-8:
                continue
            weighted_vectors.append(vector / norm * weight)
            vector_weight += weight
        embedding: tuple[float, ...] | None = None
        if weighted_vectors and vector_weight > 0 and np is not None:
            mean = np.sum(np.stack(weighted_vectors), axis=0) / vector_weight
            norm = float(np.linalg.norm(mean))
            if np.isfinite(norm) and norm > 1e-8:
                embedding = tuple(float(x) for x in (mean / norm))
        return TurnAggregate(
            embedding=embedding,
            score=max(-1.0, min(1.0, weighted_score / weight_total)),
            total_speech_ms=total_speech,
            window_count=len(eligible),
        )

    def aggregate(self) -> TurnAggregate | None:
        return self._build_aggregate(self._eligible())

    def observe(self, observation: SpeakerObservation) -> SpeakerState:
        """Accumulate one window and refresh the provisional turn state."""
        if self.state == SpeakerState.UNAVAILABLE:
            return self.state
        if not self._accept_observation(observation):
            return self.state
        self._aggregate = self.aggregate()
        self.state = self._resolve(self._aggregate)
        return self.state

    def observe_many(self, observations: Iterable[SpeakerObservation]) -> SpeakerState:
        for item in observations:
            self.observe(item)
        return self.state

    # ----------------------------------------------------------------- decide
    def _resolve(self, aggregate: TurnAggregate | None) -> SpeakerState:
        if aggregate is None or aggregate.total_speech_ms < self.config.min_turn_speech_ms:
            # 尚未形成足够证据也按非主人输出，不授权主人权限。
            return SpeakerState.NON_OWNER_CONFIRMED
        now = time.monotonic()
        score = self._turn_score(aggregate)
        if score >= self.config.confirm_threshold:
            evidence = (self.turn_id, self.generation, self._evidence_sequence)
            if evidence != self._last_confirmed_evidence:
                self._last_owner_confirmed_at = now
                self._last_confirmed_evidence = evidence
            return SpeakerState.OWNER_CONFIRMED
        # 有效分数二分：连续主人只在现有时限内使用放宽后的单一阈值。
        if self._last_owner_confirmed_at is not None:
            age_ms = (now - self._last_owner_confirmed_at) * 1000
            limit = self.config.continuity_max_ms if self.state == SpeakerState.OWNER_CONTINUITY \
                else self.config.continuity_window_ms
            if age_ms <= limit and score >= self.config.continuity_threshold:
                return SpeakerState.OWNER_CONTINUITY
        self._last_owner_confirmed_at = None
        return SpeakerState.NON_OWNER_CONFIRMED

    def decision(self, *, route: str | None = None, degraded: bool = False,
                 reason: str | None = None,
                 similarity: float | None = None) -> SpeakerDecision:
        self._aggregate = self.aggregate()
        state = SpeakerState.UNAVAILABLE if self.state == SpeakerState.UNAVAILABLE else self._resolve(self._aggregate)
        if state != SpeakerState.UNAVAILABLE:
            self.state = state
        # 模型异常及证据不足也按非主人输出，原因与 degraded（降级标记）继续保留。
        if state in {SpeakerState.UNKNOWN, SpeakerState.UNAVAILABLE}:
            degraded = degraded or state == SpeakerState.UNAVAILABLE
            reason = reason or self._unavailable_reason or "insufficient_speaker_evidence"
            state = SpeakerState.NON_OWNER_CONFIRMED
        if state == SpeakerState.OWNER_CONFIRMED:
            final_route = "main"
            final_reason = reason or "owner_confirmed"
        elif state == SpeakerState.OWNER_CONTINUITY:
            final_route = "main"
            final_reason = reason or "owner_continuity"
        elif state == SpeakerState.NON_OWNER_CONFIRMED:
            final_route = "ambient"
            final_reason = reason or ("insufficient_speaker_evidence" if self._aggregate is None else "non_owner_confirmed")
        elif state == SpeakerState.UNAVAILABLE:
            final_route = "passthrough"
            final_reason = reason or self._unavailable_reason or "speaker_unavailable"
            degraded = True
        else:
            final_route = "passthrough"
            final_reason = reason or "speaker_unknown"
        if route is not None:
            final_route = route
        threshold = self.config.continuity_threshold if state == SpeakerState.OWNER_CONTINUITY \
            else self.config.confirm_threshold
        aggregate = self._aggregate
        resolved_similarity = similarity
        if resolved_similarity is None:
            resolved_similarity = self._turn_score(aggregate) if aggregate is not None else None
        return SpeakerDecision(
            turn_id=self.turn_id,
            generation=self.generation,
            user_id=self.user_id if state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY} else None,
            state=state,
            similarity=resolved_similarity,
            threshold_used=threshold if aggregate is not None else None,
            speech_ms=aggregate.total_speech_ms if aggregate is not None else 0,
            observation_count=len(self.observations),
            continuity_source="previous_owner_confirmation" if state == SpeakerState.OWNER_CONTINUITY else None,
            route=final_route,
            reason=final_reason,
            degraded=degraded,
            aggregated_windows=aggregate.window_count if aggregate is not None else 0,
        )
