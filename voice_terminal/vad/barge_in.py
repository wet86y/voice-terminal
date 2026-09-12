from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True)
class BargeInDecision:
    candidate: bool
    confirmed: bool
    duration_ms: int
    reason: str


class BargeInGate:
    """Confirm sustained near-end speech before interrupting playback."""

    def __init__(self, *, min_speech_ms: int = 250, enabled: bool = True) -> None:
        if min_speech_ms <= 0:
            raise ValueError("min_speech_ms must be positive")
        self.min_speech_ms = min_speech_ms
        self.enabled = enabled
        self._started_at: float | None = None
        self._last_at: float | None = None
        self._last_was_speech = False
        self._valid_speech_ms = 0
        self._confirmed = False

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def candidate_started_at(self) -> float | None:
        return self._started_at

    def reset(self) -> None:
        self._started_at = None
        self._last_at = None
        self._last_was_speech = False
        self._valid_speech_ms = 0
        self._confirmed = False

    def prime(self, started_at: float) -> None:
        """Seed a candidate from a sample-indexed VAD onset."""

        self._started_at = started_at
        self._last_at = started_at
        self._last_was_speech = True
        self._valid_speech_ms = 0
        self._confirmed = False

    def feed(
        self,
        activity: Any,
        *,
        now: float | None = None,
        near_voice: bool | None = None,
        protected_available: bool = True,
    ) -> BargeInDecision:
        current = time.monotonic() if now is None else now
        is_speech = bool(getattr(activity, "is_speech", False))
        started = bool(getattr(activity, "speech_started", False))
        ended = bool(getattr(activity, "speech_ended", False))
        if not self.enabled:
            return BargeInDecision(False, False, 0, "disabled")
        if not protected_available:
            self.reset()
            return BargeInDecision(False, False, 0, "protected_audio_unavailable")
        if near_voice is not True:
            # A gap or unknown value in the processed near-end decision must
            # never be counted toward the confirmation window.  Treating
            # ``None`` as permission was an unsafe raw-microphone fallback:
            # it let speaker echo cancel assistant speech whenever AEC was
            # disabled, starting up, or degraded.
            self.reset()
            return BargeInDecision(False, False, 0,
                                   "not_near_voice" if near_voice is False else "near_voice_unknown")
        if ended and not is_speech:
            self.reset()
            return BargeInDecision(False, False, 0, "speech_ended")
        if started and self._started_at is None:
            self._started_at = current
            self._last_at = current
            self._last_was_speech = False
        if not is_speech and self._started_at is None:
            return BargeInDecision(False, False, 0, "silence")
        if self._started_at is None and is_speech:
            self._started_at = current
            self._last_at = current
            self._last_was_speech = False
        # ``now`` is supplied by the capture loop, so elapsed wall time can
        # include a short VAD gap.  Count only intervals for which both the
        # current sample and the preceding observation were still speech;
        # otherwise a noise pulse plus silence could satisfy the 250 ms wall
        # clock without 200 ms of valid near-end voice.
        if self._last_at is None:
            self._last_at = current
        elif is_speech and self._last_was_speech:
            self._valid_speech_ms += max(0, int((current - self._last_at) * 1000))
        self._last_at = current
        self._last_was_speech = is_speech
        duration = self._valid_speech_ms
        if not is_speech:
            return BargeInDecision(self._started_at is not None, False, duration, "candidate")
        if not self._confirmed and duration >= self.min_speech_ms:
            self._confirmed = True
            return BargeInDecision(True, True, duration, "confirmed")
        return BargeInDecision(True, self._confirmed, duration, "confirmed" if self._confirmed else "candidate")
