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
        "tts.prewarm": "speech.progress",
        "speech.replaced": "speech.progress", "speech.replace_failed": "speech.progress",
        "latency.snapshot": "metrics.snapshot", "turn.latency": "metrics.snapshot",
    }
    _PUBLIC = {"runtime.snapshot", "session.state", "metrics.snapshot", "input.started", "input.updated",
               "input.finalized", "input.completed", "agent.state", "agent.trace", "context.snapshot",
               "tool.state", "agent.reasoning", "speak.preview", "speech.queued", "speech.started", "speech.delta", "speech.progress", "speech.completed", "shadow.debug.receipt", "shadow.debug.event", "error"}

    def __init__(self, conversation_id: str | Callable[[], str]) -> None:
        self._seq = 0
        self._conversation_id = conversation_id

    @property
    def conversation_id(self) -> str:
        return self._conversation_id() if callable(self._conversation_id) else self._conversation_id

    def envelope(self, event: VoiceEvent) -> dict[str, Any]:
        self._seq += 1
        payload = dict(event.payload)
        public_type = self._EVENTS.get(event.type, event.type)
        if event.turn_id:
            payload.setdefault("turn_id", event.turn_id)
        return {
            "type": public_type,
            "seq": self._seq,
            "timestamp": event.timestamp or utc_now_iso(),
            "conversation_id": self.conversation_id,
            "turn_id": payload.get("turn_id"),
            "speech_id": payload.get("speech_id"),
            "payload": payload,
        }

    def public(self, event: VoiceEvent) -> bool:
        return self._EVENTS.get(event.type, event.type) in self._PUBLIC


def snapshot_event(state: RuntimeState) -> VoiceEvent:
    return VoiceEvent(type="runtime.snapshot", turn_id=None, payload=state.snapshot())
