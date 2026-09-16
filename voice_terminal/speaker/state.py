from __future__ import annotations

import time
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .contracts import SpeakerDecision, SpeakerObservation, SpeakerState


@dataclass(frozen=True)
class SpeakerStateConfig:
    confirm_threshold: float = 0.50
    continuity_threshold: float = 0.40
    min_speech_ms: int = 500
    continuity_confirm_ms: int = 1000
    continuity_window_ms: int = 5000
    continuity_max_ms: int = 10000
    owner_confirm_windows: int = 2
    non_owner_windows: int = 2

    def __post_init__(self) -> None:
        if not 0.0 <= self.continuity_threshold < self.confirm_threshold <= 1.0:
            raise ValueError("continuity_threshold must be below confirm_threshold in [0, 1]")
        if self.min_speech_ms < 1 or self.continuity_confirm_ms < self.min_speech_ms:
            raise ValueError("speaker speech durations must be positive")
        if self.continuity_window_ms < 1 or self.continuity_max_ms < self.continuity_window_ms:
            raise ValueError("speaker continuity windows are invalid")
        if self.owner_confirm_windows < 1 or self.non_owner_windows < 1:
            raise ValueError("speaker window counts must be positive")


class SpeakerStateMachine:
    """Turn-local speaker hysteresis; raw scores never become events."""

    def __init__(self, *, config: SpeakerStateConfig | None = None,
                 user_id: str | None = "owner") -> None:
        self.config = config or SpeakerStateConfig()
        self.user_id = user_id
        self.reset()

    def reset(self, *, turn_id: str | None = None, generation: int = 0) -> None:
        self.turn_id = turn_id or ""
        self.generation = generation
        self.state = SpeakerState.UNKNOWN
        self.observations: list[SpeakerObservation] = []
        self._last_owner_confirmed_at: float | None = None
        self._continuity_started_at: float | None = None
        self._owner_streak = 0
        self._non_owner_streak = 0
        self._best_similarity: float | None = None
        self._best_template: str | None = None
        self._last_similarity: float | None = None
        self._last_template: str | None = None
        self._last_qualified_window_end: int | None = None
        self._speech_ms = 0
        self._last_activity_at: float | None = None
        self._unavailable_reason: str | None = None

    def mark_unavailable(self, reason: str = "speaker_unavailable") -> None:
        self.state = SpeakerState.UNAVAILABLE
        self._unavailable_reason = str(reason or "speaker_unavailable")[:240]

    def _expire_continuity(self, now: float | None = None) -> None:
        """Drop stale owner evidence without changing either threshold.

        The low threshold is a short-lived hold window, not a global adaptive
        threshold.  Expiry is checked both while observations arrive and when
        the final decision is requested, so a quiet turn cannot retain owner
        trust indefinitely.
        """
        if self.state not in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
            return
        now = time.monotonic() if now is None else now
        if self._last_owner_confirmed_at is None:
            self.state = SpeakerState.UNKNOWN
            self._continuity_started_at = None
            return
        if (self.state == SpeakerState.OWNER_CONTINUITY
                and self._continuity_started_at is not None
                and (now - self._continuity_started_at) * 1000 > self.config.continuity_max_ms):
            self.state = SpeakerState.UNKNOWN
            self._continuity_started_at = None
            self._owner_streak = 0
            self._non_owner_streak = 0
            return
        # Once continuity has actually been entered, its ten-second cap is
        # the governing expiry.  The five-second age check still gates a new
        # low-threshold observation in ``observe``.
        if self.state != SpeakerState.OWNER_CONTINUITY:
            age_ms = (now - self._last_owner_confirmed_at) * 1000
            if age_ms > self.config.continuity_window_ms:
                self.state = SpeakerState.UNKNOWN
                self._continuity_started_at = None
                self._owner_streak = 0
                self._non_owner_streak = 0

    def _accept_observation(self, observation: SpeakerObservation) -> bool:
        if observation.turn_id != self.turn_id or observation.generation != self.generation:
            return False
        self.observations.append(observation)
        if len(self.observations) > 64:
            self.observations.pop(0)
        self._last_activity_at = time.monotonic()
        try:
            speech_ms = int(observation.speech_ms or 0)
        except (TypeError, ValueError, OverflowError):
            speech_ms = 0
        self._speech_ms = max(self._speech_ms, speech_ms)
        score = observation.similarity
        quality = observation.quality.strip().lower() if isinstance(observation.quality, str) else "unknown"
        # Only a worker window explicitly marked ``ready`` is identity
        # evidence.  A missing/unknown quality marker must fail open rather
        # than allowing a caller-provided score to promote the owner.
        if score is None or quality != "ready":
            return True
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError):
            return True
        if not math.isfinite(score):
            return True
        score = max(-1.0, min(1.0, score))
        self._last_similarity = score
        self._last_template = observation.matched_template_id
        if self._best_similarity is None or score > self._best_similarity:
            self._best_similarity = score
            self._best_template = observation.matched_template_id
        return True

    def observe(self, observation: SpeakerObservation) -> SpeakerState:
        self._expire_continuity()
        if not self._accept_observation(observation):
            return self.state
        score = observation.similarity
        try:
            speech_ms = int(observation.speech_ms or 0)
        except (TypeError, ValueError, OverflowError):
            speech_ms = 0
        quality = observation.quality.strip().lower() if isinstance(observation.quality, str) else "unknown"
        if score is None or quality != "ready" or speech_ms < self.config.min_speech_ms:
            # An unusable window cannot help establish the two-window
            # confirmation streak.  It is not non-owner evidence either.
            self._owner_streak = 0
            self._non_owner_streak = 0
            return self.state
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError):
            self._owner_streak = 0
            self._non_owner_streak = 0
            return self.state
        if not math.isfinite(score):
            self._owner_streak = 0
            self._non_owner_streak = 0
            return self.state
        score = max(-1.0, min(1.0, score))
        try:
            window_start = int(observation.window_started_sample)
            window_end = int(observation.window_ended_sample)
        except (TypeError, ValueError, OverflowError):
            self._owner_streak = 0
            self._non_owner_streak = 0
            return self.state
        if (isinstance(observation.window_started_sample, bool)
                or isinstance(observation.window_ended_sample, bool)
                or window_start < 0 or window_end <= window_start):
            self._owner_streak = 0
            self._non_owner_streak = 0
            return self.state
        contiguous = (
            self._last_qualified_window_end is None
            or (window_start <= self._last_qualified_window_end
                and window_end >= self._last_qualified_window_end)
        )
        # Keep the furthest qualified endpoint.  An out-of-order result may
        # reset a streak, but must not move the cursor backwards and make a
        # later stale result look contiguous again.
        if (self._last_qualified_window_end is None
                or window_end > self._last_qualified_window_end):
            self._last_qualified_window_end = window_end
        now = time.monotonic()
        if score >= self.config.confirm_threshold:
            self._owner_streak = self._owner_streak + 1 if contiguous else 1
            self._non_owner_streak = 0
            # A sufficiently long first window can establish ownership on its
            # own.  A short window must instead be corroborated by a second
            # consecutive qualifying window.
            enough_single_window = speech_ms >= self.config.continuity_confirm_ms
            if self._owner_streak >= self.config.owner_confirm_windows or enough_single_window:
                self.state = SpeakerState.OWNER_CONFIRMED
                self._last_owner_confirmed_at = now
                self._continuity_started_at = None
            return self.state

        # An explicit low-quality/other-speaker observation breaks continuity
        # only after enough usable windows; one weak window must not flap.
        if score < self.config.continuity_threshold:
            self._non_owner_streak = self._non_owner_streak + 1 if contiguous else 1
            self._owner_streak = 0
            if self._non_owner_streak >= self.config.non_owner_windows:
                self.state = SpeakerState.NON_OWNER_CONFIRMED
                self._last_owner_confirmed_at = None
                self._continuity_started_at = None
            elif self.state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
                # A single usable low window is enough to end the owner's
                # continuity hold.  We still wait for the configured streak
                # before calling it a confirmed non-owner, so filter mode
                # remains fail-open during the ambiguous interval.
                self.state = SpeakerState.UNKNOWN
            return self.state

        self._owner_streak = 0
        self._non_owner_streak = 0
        if self.state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
            if self._last_owner_confirmed_at is not None:
                age_ms = (now - self._last_owner_confirmed_at) * 1000
                if age_ms <= self.config.continuity_window_ms:
                    if self._continuity_started_at is None:
                        self._continuity_started_at = now
                    if (now - self._continuity_started_at) * 1000 <= self.config.continuity_max_ms:
                        self.state = SpeakerState.OWNER_CONTINUITY
                        return self.state
        self.state = SpeakerState.UNKNOWN
        return self.state

    def observe_many(self, observations: Iterable[SpeakerObservation]) -> SpeakerState:
        for item in observations:
            self.observe(item)
        return self.state

    @property
    def best_similarity(self) -> float | None:
        return self._best_similarity

    def decision(self, *, route: str | None = None, degraded: bool = False,
                 reason: str | None = None) -> SpeakerDecision:
        self._expire_continuity()
        state = self.state
        if state == SpeakerState.OWNER_CONFIRMED:
            final_route = "main"
            final_reason = reason or "owner_confirmed"
        elif state == SpeakerState.OWNER_CONTINUITY:
            final_route = "main"
            final_reason = reason or "owner_continuity"
        elif state == SpeakerState.NON_OWNER_CONFIRMED:
            final_route = "ambient"
            final_reason = reason or "non_owner_confirmed"
        elif state == SpeakerState.UNAVAILABLE:
            final_route = "passthrough"
            final_reason = reason or self._unavailable_reason or "speaker_unavailable"
            degraded = True
        else:
            final_route = "passthrough"
            final_reason = reason or "speaker_unknown"
        if route is not None:
            final_route = route
        threshold = self.config.continuity_threshold if state == SpeakerState.OWNER_CONTINUITY else self.config.confirm_threshold
        return SpeakerDecision(
            turn_id=self.turn_id,
            generation=self.generation,
            user_id=self.user_id if state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY} else None,
            state=state,
            similarity=self._last_similarity if self._last_similarity is not None else self._best_similarity,
            threshold_used=threshold if self.observations else None,
            speech_ms=self._speech_ms,
            observation_count=len(self.observations),
            continuity_source="previous_owner_confirmation" if state == SpeakerState.OWNER_CONTINUITY else None,
            route=final_route,
            reason=final_reason,
            degraded=degraded,
        )
