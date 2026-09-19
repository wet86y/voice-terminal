from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts import SpeakerDecision, SpeakerObservation
from .engine import WeSpeakerOnnxEngine
from .registry import SpeakerProfileStore
from .state import SpeakerStateConfig, SpeakerStateMachine, TurnAggregate


class SpeakerRouter:
    """Turn-aware matcher that translates embeddings into stable decisions.

    The enrolled centroid is the only reference.  Each window is scored for
    diagnostics, but the authorising score is a single cosine between the
    speech-weighted mean embedding of the whole turn and the centroid, so the
    runtime and the settings-page test share exactly one formula.
    """

    def __init__(self, profile: SpeakerProfileStore, *, confirm_threshold: float = 0.60,
                 continuity_threshold: float = 0.50, min_speech_ms: int = 700,
                 min_turn_speech_ms: int = 1500) -> None:
        self.profile = profile
        self.state_config = SpeakerStateConfig(
            confirm_threshold=confirm_threshold,
            continuity_threshold=continuity_threshold,
            min_speech_ms=min_speech_ms,
            min_turn_speech_ms=min_turn_speech_ms,
        )
        self.machine = SpeakerStateMachine(
            config=self.state_config, user_id="owner", scorer=self.score_aggregate,
        )
        self.last_decision: SpeakerDecision | None = None

    def configure(self, *, confirm_threshold: float, continuity_threshold: float) -> None:
        self.state_config = SpeakerStateConfig(
            confirm_threshold=confirm_threshold,
            continuity_threshold=continuity_threshold,
            min_speech_ms=self.state_config.min_speech_ms,
            min_turn_speech_ms=self.state_config.min_turn_speech_ms,
        )
        # Threshold edits apply to subsequent windows immediately, but must
        # not erase evidence already collected for the active turn.  Keep the
        # same state machine (turn/generation, observations and the continuity
        # record) and only swap its immutable configuration object.
        self.machine.config = self.state_config

    def reset(self, *, turn_id: str, generation: int) -> None:
        self.machine.reset(turn_id=turn_id, generation=generation)
        self.last_decision = None

    def mark_unavailable(self, reason: str) -> None:
        self.machine.mark_unavailable(reason)

    def centroid(self) -> tuple[float, ...] | None:
        if not self.profile.available:
            return None
        for template_id, vector in self.profile.vectors(include_centroid=True):
            if template_id == "centroid":
                return vector
        return None

    def score_aggregate(self, aggregate: TurnAggregate) -> float | None:
        """Cosine between the turn's aggregated embedding and the centroid."""
        centre = self.centroid()
        if centre is None or not aggregate.embedding:
            return None
        if len(aggregate.embedding) != len(centre):
            # A model/profile dimension mismatch is unavailable evidence, not
            # a low-confidence non-owner observation.
            return None
        return WeSpeakerOnnxEngine.similarity(aggregate.embedding, centre)

    def score_embedding(self, embedding: tuple[float, ...]) -> float | None:
        centre = self.centroid()
        if centre is None or not embedding or len(embedding) != len(centre):
            return None
        return WeSpeakerOnnxEngine.similarity(embedding, centre)

    def observe(self, observation: SpeakerObservation) -> SpeakerObservation:
        if not self.profile.available:
            self.machine.mark_unavailable("profile_unregistered")
            return observation
        if observation.embedding:
            score = self.score_embedding(observation.embedding)
            observation = replace(observation, similarity=score,
                                  matched_template_id="centroid" if score is not None else None)
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
