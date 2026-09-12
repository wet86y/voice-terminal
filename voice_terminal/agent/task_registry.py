"""Voice-facing catalog for the single shadow-task business action."""
from __future__ import annotations

from voice_terminal.shadow_agent.contracts import task_action_definition


class VoiceTaskRegistry:
    """A schema/status adapter; task execution stays outside the Agent core."""

    def __init__(self, *, available: bool) -> None:
        self.available = available

    def begin_turn(self, _episode_id: str) -> None:
        return None

    def cancel_turn(self, _episode_id: str) -> None:
        return None

    def close(self) -> None:
        return None

    def action_definitions(self) -> list[dict]:
        return [task_action_definition()] if self.available else []

    def service_status(self, name: str) -> str:
        return "READY" if name == "task" and self.available else "DISABLED"
