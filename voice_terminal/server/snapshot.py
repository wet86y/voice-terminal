from __future__ import annotations

from typing import Any, Callable

from ..core.events import VoiceEvent, utc_now_iso
from ..core.state import RuntimeState


class ProtocolSequencer:
    """Project internal events into the single public session protocol.

    The audio pipeline deliberately has no knowledge of WebSocket vocabulary;
    this is the only translation boundary.  Legacy names never cross it.
    """
    _EVENTS = {
        "turn.started": "input.started", "transcript.partial": "input.updated",
        "transcript.segment": "input.updated", "transcript.final": "input.finalized",
        "transcript.refined": "input.finalized", "turn.completed": "input.completed",
        "agent.episode": "agent.state", "llm.context": "context.snapshot",
        "agent.tool": "tool.state", "speech.text_delta": "speech.delta",
        "tts.status": "speech.progress", "tts.progress": "speech.progress",
        "latency.snapshot": "metrics.snapshot",
    }
    _PUBLIC = {"runtime.snapshot", "session.state", "metrics.snapshot", "input.started", "input.updated",
               "input.finalized", "input.completed", "agent.state", "agent.trace", "context.snapshot",
               "tool.state", "speech.started", "speech.delta", "speech.progress", "speech.completed", "error"}

    def __init__(self, session_id: str | Callable[[], str]) -> None:
        self._seq = 0
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id() if callable(self._session_id) else self._session_id

    def envelope(self, event: VoiceEvent) -> dict[str, Any]:
        self._seq += 1
        payload = dict(event.payload)
        public_type = self._EVENTS.get(event.type, event.type)
        # `turn_id` was an overloaded legacy identity. It is an input identity
        # at the public boundary; the more specific IDs remain in payload.
        if event.turn_id:
            payload.setdefault("input_id", event.turn_id)
        if "agent_step_id" in payload:
            payload.setdefault("decision_id", payload["agent_step_id"])
        return {
            "type": public_type,
            "seq": self._seq,
            "timestamp": event.timestamp or utc_now_iso(),
            "session_id": self.session_id,
            "input_id": payload.get("input_id"),
            "episode_id": payload.get("episode_id"),
            "decision_id": payload.get("decision_id"),
            "speech_id": payload.get("speech_id"),
            "payload": payload,
        }

    def public(self, event: VoiceEvent) -> bool:
        return self._EVENTS.get(event.type, event.type) in self._PUBLIC


def snapshot_event(state: RuntimeState) -> VoiceEvent:
    return VoiceEvent(type="runtime.snapshot", turn_id=None, payload=state.snapshot())
