"""Authoritative, Shadow-specific context projection."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import ShadowContextFrame, ShadowContextSnapshot, ShadowContextUpdate


class ShadowContextProjector:
    """Expose semantic session facts without the voice runtime prompt/tail.

    The environment frame is frozen when the session projector is created so
    it cannot disturb the cache prefix on later wakes.  The ledger owns the
    monotonic input revision and deliberately excludes Shadow-origin events.
    """

    def __init__(self, ledger: Any, *, user_profile: str = "") -> None:
        self.ledger = ledger
        now = datetime.now().astimezone()
        self._environment = ShadowContextFrame(
            revision=0, origin="runtime", kind="session_environment",
            content={
                "date": now.date().isoformat(),
                "timezone": str(now.tzinfo),
                "user_profile": str(user_profile or "").strip(),
            },
        )

    @property
    def shadow_input_revision(self) -> int:
        return self.ledger.shadow_input_revision

    @property
    def shadow_context_epoch(self) -> int:
        return self.ledger.shadow_context_epoch

    def shadow_context_snapshot(self) -> ShadowContextSnapshot:
        snapshot = self.ledger.shadow_context_snapshot()
        return ShadowContextSnapshot(
            epoch=snapshot.epoch, revision=snapshot.revision,
            frames=(self._environment, *snapshot.frames),
        )

    def shadow_context_update(
        self, since_revision: int, *, epoch: int,
    ) -> ShadowContextUpdate | ShadowContextSnapshot:
        update = self.ledger.shadow_context_update(since_revision, epoch=epoch)
        if isinstance(update, ShadowContextSnapshot):
            return ShadowContextSnapshot(
                epoch=update.epoch, revision=update.revision,
                frames=(self._environment, *update.frames),
            )
        return update

    def shadow_compaction_snapshot(self, *, recent_interactions: int = 10) -> ShadowContextSnapshot:
        """Keep recent dialogue verbatim; older semantics live in the model summary."""
        snapshot = self.ledger.shadow_context_snapshot()
        frames = list(snapshot.frames)
        interaction_indexes = [
            index for index, frame in enumerate(frames)
            if frame.origin in {"user", "main_agent"}
        ]
        keep_count = max(1, recent_interactions * 2)
        start = interaction_indexes[-keep_count] if len(interaction_indexes) > keep_count else 0
        return ShadowContextSnapshot(
            epoch=snapshot.epoch, revision=snapshot.revision,
            frames=(self._environment, *frames[start:]),
        )

    def set_shadow_runtime_state(self, *, available: bool | None = None, active: bool | None = None) -> None:
        setter = getattr(self.ledger, "set_shadow_runtime_state", None)
        if callable(setter):
            setter(available=available, active=active)
