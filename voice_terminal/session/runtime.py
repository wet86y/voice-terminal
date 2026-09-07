"""Conversation identity and stale-result authority for the V0.5 runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from .contracts import new_id


logger = logging.getLogger(__name__)


class EpochReason(StrEnum):
    USER_RESUMED = "USER_RESUMED"
    INPUT_FINALIZED = "INPUT_FINALIZED"
    INPUT_REFINED = "INPUT_REFINED"
    USER_INTERRUPTED = "USER_INTERRUPTED"
    TOOL_RESULT_ACCEPTED = "TOOL_RESULT_ACCEPTED"
    ASSISTANT_DELIVERED = "ASSISTANT_DELIVERED"
    RESET = "RESET"


@dataclass(frozen=True)
class ValidityToken:
    turn_id: str
    epoch: int
    speech_id: str | None = None


class ConversationRuntime:
    """The one authority for conversation identity and asynchronous validity.

    IDs name durable business objects; ``epoch`` names a semantic world.  No
    transport, player, or request-local counter participates in this check.
    """

    def __init__(self) -> None:
        self.conversation_id = new_id("conversation")
        self.epoch = 0
        self.current_turn_id: str | None = None

    def begin_turn(self, turn_id: str) -> ValidityToken:
        self.current_turn_id = turn_id
        return self.token(turn_id)

    def token(self, turn_id: str | None = None, *, speech_id: str | None = None) -> ValidityToken:
        value = turn_id or self.current_turn_id
        if not value:
            raise ValueError("a current turn is required for a validity token")
        return ValidityToken(value, self.epoch, speech_id)

    def advance_epoch(self, reason: EpochReason) -> int:
        previous = self.epoch
        self.epoch += 1
        logger.info("conversation.epoch advanced from=%s to=%s reason=%s conversation_id=%s turn_id=%s",
                    previous, self.epoch, reason.value, self.conversation_id, self.current_turn_id)
        return self.epoch

    def is_current(self, turn_id: str, epoch: int) -> bool:
        return self.current_turn_id == turn_id and self.epoch == epoch

    def is_current_speech(self, turn_id: str, epoch: int, speech_id: str, active_speech_id: str | None) -> bool:
        return self.is_current(turn_id, epoch) and active_speech_id == speech_id

    def reset(self) -> None:
        self.advance_epoch(EpochReason.RESET)
        self.conversation_id = new_id("conversation")
        self.current_turn_id = None
