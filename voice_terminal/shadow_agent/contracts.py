"""Public command and event contracts for the isolated shadow task harness."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


TaskCommandState = Literal["request", "modify", "cancel"]
TaskEventStatus = Literal["running", "completed", "cancelled", "timed_out", "failed"]


@dataclass(frozen=True)
class ShadowTaskCommand:
    id: int
    state: TaskCommandState
    text: str

    @classmethod
    def parse(cls, value: Any) -> "ShadowTaskCommand":
        if not isinstance(value, dict) or set(value) != {"id", "state", "text"}:
            raise ValueError("task command requires exactly id, state, and text")
        identifier = value["id"]
        if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 1:
            raise ValueError("task.id must be a positive integer")
        state = value["state"]
        if state not in {"request", "modify", "cancel"}:
            raise ValueError("task.state must be request, modify, or cancel")
        text = value["text"]
        if not isinstance(text, str):
            raise ValueError("task.text must be a string")
        return cls(identifier, state, text)


@dataclass(frozen=True)
class ShadowTaskEvent:
    id: int
    status: TaskEventStatus
    text: str


@dataclass(frozen=True)
class TaskReceipt:
    accepted: bool
    id: int
    status: str
    message: str


def task_action_definition() -> dict[str, Any]:
    """Return the future voice-facing action definition without registering it.

    Responses strict schemas require every declared argument property to be
    required.  Empty text is therefore the schema-compatible representation
    for cancel and in-flight status requests.
    """

    return {
        "name": "task",
        "description": "Create, query, modify, or cancel one shadow task.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "state", "text"],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "state": {"type": "string", "enum": ["request", "modify", "cancel"]},
                "text": {"type": "string"},
            },
        },
    }
