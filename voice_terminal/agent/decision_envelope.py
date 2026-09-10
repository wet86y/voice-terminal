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
# A small compatibility set for ordinary stylistic labels emitted by the
# model.  These values do not change speech content and have an unambiguous
# safe provider fallback, so treating them as a fatal envelope error only
# creates an avoidable recovery round-trip.
EMOTION_ALIASES = {"friendly": "calm"}
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
    """Collect JSON-mode content with a staged validation boundary.

    ``feed`` still exposes only text belonging to a locally valid ``speak``
    prefix.  The caller may now commit those deltas to the text-only speech
    queue immediately; :meth:`finish` remains authoritative for the complete
    envelope and its actions.
    """

    def __init__(self, *, limit: int = 32768, allow_empty_control_text: bool = False) -> None:
        self.raw = ""
        self.limit = limit
        self.allow_empty_control_text = allow_empty_control_text
        self.state: str | None = None
        self._emitted_length = 0
        self.emotion_normalizations: list[dict[str, str]] = []

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

    def _emotion(self, value: str | None) -> str | None:
        """Validate an emotion and safely normalize known benign aliases."""

        if value is None:
            return None
        if value in EMOTIONS:
            return value
        mapped = EMOTION_ALIASES.get(value.casefold())
        if mapped is None:
            raise EnvelopeError("invalid speak emotion")
        if not any(item["provided"] == value for item in self.emotion_normalizations):
            self.emotion_normalizations.append({"provided": value, "applied": mapped})
        return mapped

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
        self._emotion(self._quoted_value(self.raw, "emotion"))
        if self.state != "speak":
            return ""
        text = self._quoted_value(self.raw, "text", allow_partial=True)
        if text is None or len(text) <= self._emitted_length:
            return ""
        value = text[self._emitted_length:]
        self._emitted_length = len(text)
        return value

    def speak_prefix(self) -> SpeechDecision | None:
        """Return the currently safe speak prefix, if one exists.

        This is intentionally weaker than ``finish``: it does not inspect the
        top-level envelope or actions.  It only authorizes a text transaction
        after the state/emotion are known to be valid and a non-empty text
        prefix has arrived.
        """
        state = self.state
        if state is None:
            return None
        emotion = self._emotion(self._quoted_value(self.raw, "emotion")) or "calm"
        if state != "speak":
            return SpeechDecision(state, emotion, "")
        text = self._quoted_value(self.raw, "text", allow_partial=True)
        if not isinstance(text, str) or not text.strip():
            return None
        return SpeechDecision(state, emotion, text)

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
        raw_emotion = speak.get("emotion", "calm")
        if not isinstance(raw_emotion, str):
            raise EnvelopeError("invalid speak emotion")
        emotion = self._emotion(raw_emotion)
        if emotion is None:  # Defensive; a complete speak object always has a default above.
            emotion = "calm"
        has_text = "text" in speak
        text = speak.get("text", "")
        if not isinstance(text, str):
            raise EnvelopeError("speak text must be a string")
        if state == "speak" and not text.strip():
            raise EnvelopeError("speak requires nonempty text")
        if state != "speak" and has_text and not (self.allow_empty_control_text and text == ""):
            raise EnvelopeError("cancel/wait cannot contain text")
        actions = value.get("actions")
        if not isinstance(actions, list) or len(actions) > 8:
            raise EnvelopeError("actions must be an array with at most eight entries")
        return DecisionEnvelope(SpeechDecision(state, emotion, text), tuple(self._action(item) for item in actions))
