"""Contracts for the session-scoped single Shadow Agent runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


ShadowEventKind = Literal[
    "status", "progress", "observation", "user_input_required", "result", "failed",
]
WakePolicy = Literal["passive", "normal", "priority"]
ContextOrigin = Literal["user", "main_agent", "external_system", "runtime"]
WakeResult = Literal["started", "coalesced", "already_current", "unavailable"]


class ToolEffect(StrEnum):
    """Effect declaration frozen with every registered Shadow tool."""

    READ_ONLY = "READ_ONLY"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    EXTERNAL_COMMIT = "EXTERNAL_COMMIT"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(frozen=True)
class ShadowContextFrame:
    revision: int
    origin: ContextOrigin
    kind: str
    content: Any


@dataclass(frozen=True)
class ShadowContextSnapshot:
    epoch: int
    revision: int
    frames: tuple[ShadowContextFrame, ...]


@dataclass(frozen=True)
class ShadowContextUpdate:
    epoch: int
    from_revision: int
    to_revision: int
    frames: tuple[ShadowContextFrame, ...]


@dataclass(frozen=True)
class ShadowEvent:
    event_id: str
    kind: ShadowEventKind
    text: str
    based_on_context_revision: int
    wake_policy: WakePolicy
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShadowVisibleStatus:
    active: bool
    text: str
    based_on_trace_revision: int
    updated_at: float


def parse_shadow_wake(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"request"} or value["request"] is not True:
        raise ValueError('request_shadow requires exactly {"request": true}')
    return True


def request_shadow_action_definition() -> dict[str, Any]:
    return {
        "name": "request_shadow",
        "description": "让独立影子 Agent 同步最新完整会话上下文，并推进当前仍需处理的工作。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request"],
            "properties": {"request": {"type": "boolean", "const": True}},
        },
    }


def shadow_control_tool_definitions() -> list[dict[str, Any]]:
    """Return the fixed, local-only tools appended after business tools."""

    return [
        {
            "type": "function", "name": "get_latest_context",
            "description": "读取指定会话修订之后的最新权威上下文。",
            "parameters": {
                "type": "object", "additionalProperties": False, "required": ["since_revision"],
                "properties": {"since_revision": {"type": "integer", "minimum": 0}},
            },
        },
        {
            "type": "function", "name": "publish_main_event",
            "description": "向主助手发布可覆盖的进度或具有会话意义的新发现。",
            "parameters": {
                "type": "object", "additionalProperties": False, "required": ["kind", "text"],
                "properties": {
                    "kind": {"type": "string", "enum": ["progress", "observation"]},
                    "text": {"type": "string", "minLength": 1},
                },
            },
        },
        {
            "type": "function", "name": "request_user_input",
            "description": "请求主助手自然地向用户澄清；confirmation 仅保留契约，首版不得执行外部副作用。",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "question", "options", "operation_summary"],
                "properties": {
                    "kind": {"type": "string", "enum": ["clarification", "confirmation"]},
                    "question": {"type": "string", "minLength": 1},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "operation_summary": {"type": "string"},
                },
            },
        },
    ]
