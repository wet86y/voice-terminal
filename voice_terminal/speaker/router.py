from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts import SpeakerDecision, SpeakerObservation, SpeakerState
from .engine import WeSpeakerOnnxEngine
from .registry import SpeakerProfileStore
from .state import SpeakerStateConfig, SpeakerStateMachine


class SpeakerRouter:
    """Turn-aware matcher that translates embeddings into stable decisions."""

    def __init__(self, profile: SpeakerProfileStore, *, confirm_threshold: float = 0.50,
                 continuity_threshold: float = 0.40, min_speech_ms: int = 500) -> None:
        self.profile = profile
        self.state_config = SpeakerStateConfig(
            confirm_threshold=confirm_threshold,
            continuity_threshold=continuity_threshold,
            min_speech_ms=min_speech_ms,
        )
        self.machine = SpeakerStateMachine(config=self.state_config, user_id="owner")
        self.last_decision: SpeakerDecision | None = None

    def configure(self, *, confirm_threshold: float, continuity_threshold: float) -> None:
        self.state_config = SpeakerStateConfig(
            confirm_threshold=confirm_threshold,
            continuity_threshold=continuity_threshold,
            min_speech_ms=self.state_config.min_speech_ms,
        )
        # Threshold edits apply to subsequent windows immediately, but must
        # not erase evidence already collected for the active turn.  Keep the
        # same state machine (turn/generation, observations and hysteresis
        # counters) and only swap its immutable configuration object.
        self.machine.config = self.state_config

    def reset(self, *, turn_id: str, generation: int) -> None:
        self.machine.reset(turn_id=turn_id, generation=generation)
        self.last_decision = None

    def mark_unavailable(self, reason: str) -> None:
        self.machine.mark_unavailable(reason)

    def _match(self, embedding: tuple[float, ...]) -> tuple[float | None, str | None]:
        if not self.profile.available:
            return None, None
        # The centroid is authoritative.  A special template is consulted only
        # when the centroid is in the fuzzy band; it must not bypass a clearly
        # low centre score and thereby expand the owner's identity by accident.
        vectors = self.profile.vectors(include_centroid=True)
        centroid = next(((template_id, vector) for template_id, vector in vectors
                         if template_id == "centroid"), None)
        if centroid is None:
            return None, None
        if not embedding or len(embedding) != len(centroid[1]):
            # A model/profile dimension mismatch is unavailable evidence, not
            # a low-confidence non-owner observation.
            return None, None
        centre_score = WeSpeakerOnnxEngine.similarity(embedding, centroid[1])
        if centre_score >= self.state_config.confirm_threshold:
            return centre_score, "centroid"
        if centre_score < self.state_config.continuity_threshold:
            return centre_score, "centroid"
        special_scores = [
            (WeSpeakerOnnxEngine.similarity(embedding, vector), template_id)
            for template_id, vector in vectors
            if template_id != "centroid" and len(vector) == len(embedding)
        ]
        if not special_scores:
            return centre_score, "centroid"
        best = max(special_scores, key=lambda item: item[0])
        return best if best[0] > centre_score else (centre_score, "centroid")

    def observe(self, observation: SpeakerObservation) -> SpeakerObservation:
        if not self.profile.available:
            self.machine.mark_unavailable("profile_unregistered")
            return observation
        if observation.embedding:
            score, template_id = self._match(observation.embedding)
            observation = replace(observation, similarity=score, matched_template_id=template_id)
        self.machine.observe(observation)
        return observation

    def decision(self, *, route: str | None = None, degraded: bool = False,
                 reason: str | None = None) -> SpeakerDecision:
        self.last_decision = self.machine.decision(route=route, degraded=degraded, reason=reason)
        return self.last_decision

    def snapshot(self) -> dict[str, Any]:
        decision = self.last_decision or self.machine.decision()
        return {
            "decision": decision.to_payload(),
            "observations": [item.to_payload() for item in self.machine.observations[-8:]],
            "profile_available": self.profile.available,
        }
