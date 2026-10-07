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

    def __init__(self, ledger: Any, *, user_profile: str = "", continuity_document: str = "",
                 custom_instructions: list[dict[str, str]] | None = None) -> None:
        self.ledger = ledger
        # The experimental context gate never filters semantic frames.  It
        # only controls the contiguous source revision currently visible to
        # Shadow.  Keeping a watermark rather than a second frame store makes
        # a later release include every previously deferred frame in order.
        self._gate_enabled = False
        self._visible_revision = int(getattr(ledger, "shadow_input_revision", 0))
        self._gate_generation = 0
        self._known_epoch = int(getattr(ledger, "shadow_context_epoch", 0))
        now = datetime.now().astimezone()
        self._continuity_document = str(continuity_document or "").strip()
        environment = {
            "date": now.date().isoformat(),
            "timezone": str(now.tzinfo),
            "user_profile": str(user_profile or "").strip(),
        }
        if self._continuity_document:
            environment["continuity_document"] = self._continuity_document
        if custom_instructions:
            environment["custom_instructions"] = [dict(item) for item in custom_instructions]
        self._environment = ShadowContextFrame(
            revision=0, origin="runtime", kind="session_environment",
            content=environment,
        )

    def set_continuity_document(self, value: str) -> None:
        """Refresh the current handoff note and publish its Shadow revision."""
        text = str(value or "").strip()
        if text == self._continuity_document:
            return
        self._continuity_document = text
        content = dict(self._environment.content)
        if text:
            content["continuity_document"] = text
        else:
            content.pop("continuity_document", None)
        self._environment = ShadowContextFrame(
            revision=0, origin="runtime", kind="session_environment", content=content,
        )
        commit = getattr(self.ledger, "commit_runtime_context", None)
        if callable(commit):
            commit("continuity_document", {"document": text})

    def environment_snapshot(self) -> dict[str, Any]:
        """environment_snapshot（固定会话环境）：随当前语音会话保存。"""
        return dict(self._environment.content)

    def restore_environment(self, content: dict[str, Any]) -> None:
        """restore_environment（恢复固定环境）：重启不改变既有上下文前缀。"""
        self._environment = ShadowContextFrame(revision=0, origin="runtime", kind="session_environment", content=dict(content))
        self._continuity_document = str(content.get("continuity_document") or "")

    @property
    def shadow_input_revision(self) -> int:
        self._sync_epoch()
        if not self._gate_enabled:
            self._visible_revision = self.source_revision
        return min(self._visible_revision, self.source_revision)

    @property
    def source_revision(self) -> int:
        return int(self.ledger.shadow_input_revision)

    @property
    def visible_revision(self) -> int:
        return self.shadow_input_revision

    @property
    def gate_enabled(self) -> bool:
        return self._gate_enabled

    @property
    def gate_generation(self) -> int:
        return self._gate_generation

    def _sync_epoch(self) -> None:
        epoch = int(self.ledger.shadow_context_epoch)
        if epoch == self._known_epoch:
            return
        self._known_epoch = epoch
        self._visible_revision = 0 if self._gate_enabled else self.source_revision
        self._gate_generation += 1

    def set_gate_enabled(self, enabled: bool) -> bool:
        """Toggle the experimental gate without rewriting source context."""
        self._sync_epoch()
        value = bool(enabled)
        if value == self._gate_enabled:
            return False
        # Enabling starts from the context that was already visible.  Disabling
        # restores legacy passthrough and releases the complete source prefix.
        self._gate_enabled = value
        if not value:
            self._visible_revision = self.source_revision
        else:
            self._visible_revision = min(self._visible_revision, self.source_revision)
        self._gate_generation += 1
        return True

    def release_through(self, revision: int | None = None) -> int:
        self._sync_epoch()
        target = self.source_revision if revision is None else int(revision)
        target = max(self._visible_revision, min(target, self.source_revision))
        self._visible_revision = target
        return target

    def candidate_update(self, *, to_revision: int | None = None) -> ShadowContextUpdate:
        """Return the unresolved authoritative interval without exposing it."""
        self._sync_epoch()
        target = self.source_revision if to_revision is None else min(int(to_revision), self.source_revision)
        raw = self.ledger.shadow_context_update(self._visible_revision, epoch=self.shadow_context_epoch)
        if isinstance(raw, ShadowContextSnapshot):
            frames = tuple(
                frame for frame in raw.frames
                if self._visible_revision < frame.revision <= target
            )
        else:
            frames = tuple(frame for frame in raw.frames if frame.revision <= target)
        return ShadowContextUpdate(
            epoch=self.shadow_context_epoch,
            from_revision=self._visible_revision,
            to_revision=target,
            frames=frames,
        )

    @property
    def shadow_context_epoch(self) -> int:
        return self.ledger.shadow_context_epoch

    def shadow_context_snapshot(self) -> ShadowContextSnapshot:
        snapshot = self.ledger.shadow_context_snapshot()
        visible = self.shadow_input_revision
        return ShadowContextSnapshot(
            epoch=snapshot.epoch, revision=visible,
            frames=(self._environment, *(frame for frame in snapshot.frames if frame.revision <= visible)),
        )

    def shadow_context_update(
        self, since_revision: int, *, epoch: int,
    ) -> ShadowContextUpdate | ShadowContextSnapshot:
        visible = self.shadow_input_revision
        if epoch == self.shadow_context_epoch and since_revision >= visible:
            return ShadowContextUpdate(
                epoch=epoch,
                from_revision=since_revision,
                to_revision=since_revision,
                frames=(),
            )
        update = self.ledger.shadow_context_update(since_revision, epoch=epoch)
        if isinstance(update, ShadowContextSnapshot):
            return ShadowContextSnapshot(
                epoch=update.epoch, revision=visible,
                frames=(self._environment, *(frame for frame in update.frames if frame.revision <= visible)),
            )
        return ShadowContextUpdate(
            epoch=update.epoch,
            from_revision=update.from_revision,
            to_revision=visible,
            frames=tuple(frame for frame in update.frames if frame.revision <= visible),
        )

    def shadow_compaction_snapshot(self, *, recent_interactions: int = 10) -> ShadowContextSnapshot:
        """Keep recent dialogue verbatim; older semantics live in the model summary."""
        snapshot = self.ledger.shadow_context_snapshot()
        visible = self.shadow_input_revision
        frames = [frame for frame in snapshot.frames if frame.revision <= visible]
        interaction_indexes = [
            index for index, frame in enumerate(frames)
            if frame.origin in {"user", "main_agent"}
        ]
        keep_count = max(1, recent_interactions * 2)
        start = interaction_indexes[-keep_count] if len(interaction_indexes) > keep_count else 0
        return ShadowContextSnapshot(
            epoch=snapshot.epoch, revision=visible,
            frames=(self._environment, *frames[start:]),
        )

    def set_shadow_runtime_state(self, *, available: bool | None = None, active: bool | None = None,
                                 activation_id: str | None = None) -> None:
        setter = getattr(self.ledger, "set_shadow_runtime_state", None)
        if callable(setter):
            setter(available=available, active=active, activation_id=activation_id)
