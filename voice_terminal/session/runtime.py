"""Conversation identity and explicit cancellation ownership."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from .contracts import new_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CancellationScope:
    scope_id: str
    turn_id: str | None


class ConversationRuntime:
    """Session identity plus explicit cancellation scope ownership."""

    def __init__(self) -> None:
        self.conversation_id = new_id("conversation")
        self.current_turn_id: str | None = None
        self._scope_id = new_id("scope")

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def begin_turn(self, turn_id: str) -> CancellationScope:
        self.current_turn_id = turn_id
        self._scope_id = new_id("scope")
        return CancellationScope(self._scope_id, turn_id)

    def cancel(self, *, turn_id: str | None = None, reason: str = "cancelled") -> CancellationScope:
        if turn_id is None or turn_id == self.current_turn_id:
            self._scope_id = new_id("scope")
            logger.info("conversation.scope_cancelled scope_id=%s reason=%s conversation_id=%s turn_id=%s",
                        self._scope_id, reason, self.conversation_id, self.current_turn_id)
        return CancellationScope(self._scope_id, self.current_turn_id)

    def is_scope_current(self, scope_id: str, turn_id: str | None = None) -> bool:
        return (scope_id == self._scope_id
                and (turn_id is None or turn_id == self.current_turn_id))

    def reset(self) -> None:
        self._scope_id = new_id("scope")
        self.conversation_id = new_id("conversation")
        self.current_turn_id = None
