"""Public command and event contracts for the isolated shadow task harness."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


TaskCommandState = Literal["request", "modify", "reply", "cancel"]
TaskEventStatus = Literal[
    "running", "awaiting_user", "paused", "completed", "cancelled", "timed_out", "failed",
]


@dataclass(frozen=True)
class ShadowTaskCommand:
    id: int
    state: TaskCommandState
    text: str
    question_id: str

    @classmethod
    def parse(cls, value: Any) -> "ShadowTaskCommand":
        if not isinstance(value, dict) or set(value) != {"id", "state", "text", "question_id"}:
            raise ValueError("task command requires exactly id, state, text, and question_id")
        identifier = value["id"]
        if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 1:
            raise ValueError("task.id must be a positive integer")
        state = value["state"]
        if state not in {"request", "modify", "reply", "cancel"}:
            raise ValueError("task.state must be request, modify, reply, or cancel")
        text = value["text"]
        if not isinstance(text, str):
            raise ValueError("task.text must be a string")
        question_id = value["question_id"]
        if not isinstance(question_id, str):
            raise ValueError("task.question_id must be a string")
        if state == "reply":
            if not text.strip() or not question_id.strip():
                raise ValueError("task.reply requires nonempty text and question_id")
        elif question_id:
            raise ValueError("task.question_id must be empty unless state=reply")
        return cls(identifier, state, text, question_id)


@dataclass(frozen=True)
class ShadowTaskEvent:
    id: int
    status: TaskEventStatus
    text: str
    question_id: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskReceipt:
    accepted: bool
    id: int
    status: str
    message: str


def task_action_definition() -> dict[str, Any]:
    """Return the future voice-facing action definition without registering it.

    Responses strict schemas require every declared argument property to be
    required. Empty strings are therefore the schema-compatible representation
    for fields unused by a command state.
    """

    return {
        "name": "task",
        "description": "Create, query, modify, reply to, or cancel one shadow task.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "state", "text", "question_id"],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "state": {"type": "string", "enum": ["request", "modify", "reply", "cancel"]},
                "text": {"type": "string"},
                "question_id": {"type": "string"},
            },
        },
    }
