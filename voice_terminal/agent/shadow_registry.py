"""Voice-facing catalog for the fixed Shadow wake action."""
from __future__ import annotations

from voice_terminal.shadow_agent.contracts import request_shadow_action_definition
from .listening import LISTENING_ACTION
from copy import deepcopy


class VoiceShadowRegistry:
    """Expose only a content-free wake when the runtime is available."""

    def __init__(self, *, available: bool, capture_service=None, supports_images=False) -> None:
        self.available = available
        self.capture_service = capture_service
        self.supports_images = supports_images

    def begin_turn(self, _episode_id: str) -> None:
        return None

    def cancel_turn(self, _episode_id: str) -> None:
        return None

    def close(self) -> None:
        return None

    def action_definitions(self) -> list[dict]:
        result = [request_shadow_action_definition()] if self.available else []
        result.append(deepcopy(LISTENING_ACTION))
        if self.supports_images:
            result.append({"name": "capture_screen", "description": "按用户需求观察当前前台应用屏幕，返回全局与局部双图；收到截图本身不构成再次截屏理由。",
                "parameters": {"type": "object", "properties": {"request": {"type": "boolean", "const": True, "description": "请求执行标记，固定为真"}},
                               "required": ["request"], "additionalProperties": False}})
        return result

    def service_status(self, name: str) -> str:
        if name == "continue_listening":
            return "READY"
        if name == "capture_screen":
            return "READY" if self.supports_images and self.capture_service and self.capture_service.available else "DISABLED"
        return "READY" if name == "request_shadow" and self.available else "DISABLED"
