"""Contracts for the session-scoped single Shadow Agent runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


ShadowEventKind = Literal[
    "progress", "information", "input_required", "result", "no_action", "failed",
]
ShadowInformationSource = Literal["agent", "observer"]
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
    event_id: str  # 事件标识：用于 Bridge 回调幂等去重。
    kind: ShadowEventKind  # 事件类型：区分界面进度、可替换信息及终态语义。
    text: str  # 文本正文：按事件类型采用相应长度上限。
    based_on_context_revision: int  # 上下文修订号：表示事件依据的权威输入版本。
    activation_id: str = ""  # 激活标识：将事件限制在一次有效任务激活内。
    source: ShadowInformationSource = "agent"  # 来源：根 Agent 或状态观察器。
    options: tuple[str, ...] = ()  # 选项列表：仅供需要用户输入的终态使用。
    failure_code: str | None = None  # 失败代码：仅供失败终态记录。
    infrastructure: bool = False  # 基础设施标记：指示失败是否属于基础设施故障。


@dataclass(frozen=True)
class DurableVoiceEvent:
    """A persisted ordinary DSH event, independent of Shadow activation state."""

    event_id: str  # 事件标识：用于持久信箱的幂等确认。
    text: str  # 事件文本：独立投递给语音主线的正文。
    source_session_id: str  # 来源会话标识：记录生成该持久事件的 DSH 会话。
    payload: dict[str, Any] = field(default_factory=dict)  # 附加数据：只属于持久语音事件，不属于 Bridge 终态。


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
        "description": (
            "让独立影子 Agent 在后台同步最新完整会话上下文并推进当前仍需处理的工作。"
            "调用此动作不表示当前必须向用户说话；主语音状态仍需独立选择 wait、speak 或 cancel。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request"],
            "properties": {"request": {
                "type": "boolean", "const": True,
                "description": "必须为 true，表示请求后台影子代理继续处理。",
            }},
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
                "properties": {"since_revision": {
                    "type": "integer", "minimum": 0,
                    "description": "已读取的上下文修订号，只返回其后的更新。",
                }},
            },
        },
        {
            "type": "function", "name": "publish_progress",
            "description": "发布只供产品界面显示的可覆盖进度；不会触发语音主模型推理。",
            "parameters": {
                "type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {
                    "text": {
                        "type": "string", "minLength": 1, "maxLength": 512,
                        "description": "仅显示在产品界面的进度文本，去除首尾空白后最多 512 字符。",
                    },
                },
            },
        },
        {
            "type": "function", "name": "publish_voice_information",
            "description": (
                "向语音主模型发布一段可被更新覆盖的信息并触发一次推理。"
                "信息不表示应当播报；只陈述事实及不确定性，不得指示主模型选择语音状态。"
            ),
            "parameters": {
                "type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {
                    "text": {
                        "type": "string", "minLength": 1, "maxLength": 2000,
                        "description": "供语音主模型本次推理判断是否表达的信息，去除首尾空白后最多 2000 字符。",
                    },
                },
            },
        },
        {
            "type": "function", "name": "request_user_input",
            "description": "请求主助手自然地向用户澄清；调用成功后结束当前激活。",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "required": ["question", "options"],
                "properties": {
                    "question": {
                        "type": "string", "minLength": 1, "maxLength": 1000,
                        "description": "需要向用户澄清的问题，去除首尾空白后最多 1000 字符。",
                    },
                    "options": {
                        "type": "array", "maxItems": 8,
                        "description": "可选回答，最多 8 项；空数组表示不提供候选项。",
                        "items": {
                            "type": "string", "minLength": 1, "maxLength": 200,
                            "description": "一个候选回答，去除首尾空白后最多 200 字符。",
                        },
                    },
                },
            },
        },
        {
            "type": "function", "name": "return_shadow_result",
            "description": "发布自包含的最终结果并结束当前激活。",
            "parameters": {
                "type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {
                    "text": {
                        "type": "string", "minLength": 1, "maxLength": 8000,
                        "description": "自包含的最终结果，包含用户后续需要的完整事实，最多 8000 字符。",
                    },
                },
            },
        },
        {
            "type": "function", "name": "finish_shadow_no_action",
            "description": "确认无需产生 Voice 语义事件并结束当前激活。",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "reason": {
                        "type": "string", "minLength": 1, "maxLength": 500,
                        "description": "可选的内部结束原因，不产生语音语义事件，最多 500 字符。",
                    },
                },
            },
        },
    ]
