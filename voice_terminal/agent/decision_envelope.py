"""V0.6 streamed model-decision envelope validation.

DeepSeek's thinking endpoint is considerably more reliable with JSON mode than
with a required native tool call.  This module is deliberately provider
agnostic: it consumes ordinary streamed ``content`` and makes the final
semantic validation authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


EMOTIONS = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
SPEAK_STATES = {"speak", "cancel", "wait"}


class EnvelopeError(ValueError):
    """A model response that can be corrected by the recovery lane."""


@dataclass(frozen=True)
class SpeechDecision:
    state: str
    emotion: str = "calm"
    text: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class DecisionEnvelope:
    speak: SpeechDecision
    actions: tuple[Action, ...]


class SpeechDecisionEnvelopeStream:
    """Collect JSON-mode content while safely exposing a provisional text body.

    A caller may display/retain the returned text deltas in a private candidate
    buffer.  It must not create a speech transaction until :meth:`finish`
    succeeds.  This retains the useful streaming transfer path without a bad
    final JSON object leaking to TTS.
    """

    def __init__(self, *, limit: int = 32768) -> None:
        self.raw = ""
        self.limit = limit
        self.state: str | None = None
        self._emitted_length = 0

    @staticmethod
    def _quoted_value(raw: str, key: str, *, allow_partial: bool = False) -> str | None:
        marker = f'"{key}"'
        pos = raw.find(marker)
        if pos < 0:
            return None
        colon = raw.find(":", pos + len(marker))
        if colon < 0:
            return None
        tail = raw[colon + 1:].lstrip()
        if not tail.startswith('"'):
            return None
        escaped = False
        body: list[str] = []
        for char in tail[1:]:
            if escaped:
                body.append("\\" + char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                try:
                    return json.loads('"' + "".join(body) + '"')
                except json.JSONDecodeError:
                    return None
            else:
                body.append(char)
        if not allow_partial:
            return None
        # The JSON string is still open.  Decode its longest complete prefix
        # so a trailing escape or incomplete \\uXXXX sequence is held until a
        # later network delta completes it.  This value is presentation-only;
        # finish() remains the sole authority for a speech transaction.
        raw_body = "".join(body)
        for end in range(len(raw_body), -1, -1):
            try:
                return json.loads('"' + raw_body[:end] + '"')
            except json.JSONDecodeError:
                continue
        return None

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str):
            raise EnvelopeError("response content delta must be text")
        self.raw += delta
        if len(self.raw) > self.limit:
            raise EnvelopeError("decision envelope too large")
        state = self._quoted_value(self.raw, "state")
        if state is not None:
            if state not in SPEAK_STATES:
                raise EnvelopeError("invalid speak state")
            self.state = state
        if self.state != "speak":
            return ""
        text = self._quoted_value(self.raw, "text", allow_partial=True)
        if text is None or len(text) <= self._emitted_length:
            return ""
        value = text[self._emitted_length:]
        self._emitted_length = len(text)
        return value

    @staticmethod
    def _action(value: Any) -> Action:
        if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
            raise EnvelopeError("each action must contain exactly name and arguments")
        name, arguments = value.get("name"), value.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise EnvelopeError("invalid action shape")
        return Action(name, arguments)

    def finish(self) -> DecisionEnvelope:
        try:
            value = json.loads(self.raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise EnvelopeError("incomplete or invalid decision JSON") from exc
        if not isinstance(value, dict) or set(value) != {"speak", "actions"}:
            raise EnvelopeError("decision envelope must contain exactly speak and actions")
        speak = value.get("speak")
        if not isinstance(speak, dict) or set(speak) - {"state", "emotion", "text"}:
            raise EnvelopeError("invalid speak object")
        state = speak.get("state")
        if state not in SPEAK_STATES:
            raise EnvelopeError("invalid or missing speak state")
        emotion = speak.get("emotion", "calm")
        if emotion not in EMOTIONS:
            raise EnvelopeError("invalid speak emotion")
        has_text = "text" in speak
        text = speak.get("text", "")
        if not isinstance(text, str):
            raise EnvelopeError("speak text must be a string")
        if state == "speak" and not text.strip():
            raise EnvelopeError("speak requires nonempty text")
        if state != "speak" and has_text:
            raise EnvelopeError("cancel/wait cannot contain text")
        actions = value.get("actions")
        if not isinstance(actions, list) or len(actions) > 8:
            raise EnvelopeError("actions must be an array with at most eight entries")
        return DecisionEnvelope(SpeechDecision(state, emotion, text), tuple(self._action(item) for item in actions))
