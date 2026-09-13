"""Voice-facing catalog for the fixed Shadow wake action."""
from __future__ import annotations

from voice_terminal.shadow_agent.contracts import request_shadow_action_definition


class VoiceShadowRegistry:
    """Expose only a content-free wake when the runtime is available."""

    def __init__(self, *, available: bool) -> None:
        self.available = available

    def begin_turn(self, _episode_id: str) -> None:
        return None

    def cancel_turn(self, _episode_id: str) -> None:
        return None

    def close(self) -> None:
        return None

    def action_definitions(self) -> list[dict]:
        return [request_shadow_action_definition()] if self.available else []

    def service_status(self, name: str) -> str:
        return "READY" if name == "request_shadow" and self.available else "DISABLED"
