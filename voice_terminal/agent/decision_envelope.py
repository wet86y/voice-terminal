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
EARLY_NON_JSON_NON_WHITESPACE_LIMIT = 10


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


@dataclass(frozen=True)
class CompatibleDecisionEnvelope:
    """A validated envelope plus the exact bounded repairs applied to it."""

    envelope: DecisionEnvelope
    repairs: tuple[str, ...]


def _schema_type_matches(value: Any, expected: str) -> bool:
    """Return the JSON-type match used by the provider-facing schema audit."""

    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate_schema_subset(value: Any, schema: Any, path: str = "$") -> tuple[bool, str | None, str | None]:
    """Validate the JSON-Schema subset emitted by ``decision_schema``.

    This is diagnostic only: the streamed envelope validator and action
    adapters remain authoritative. Keeping the small provider-facing check
    local lets telemetry distinguish syntax, provider-schema and business
    failures without introducing a new runtime dependency.
    """

    if not isinstance(schema, dict):
        return True, None, None
    expected = schema.get("type")
    if isinstance(expected, str) and not _schema_type_matches(value, expected):
        return False, "TYPE_MISMATCH", path
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False, "ENUM_MISMATCH", path
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    return False, "MISSING_REQUIRED", f"{path}.{key}"
        if schema.get("additionalProperties") is False:
            extra = next((key for key in value if key not in properties), None)
            if extra is not None:
                return False, "ADDITIONAL_PROPERTY", f"{path}.{extra}"
        for key, child in value.items():
            if key in properties:
                valid, code, child_path = _validate_schema_subset(child, properties[key], f"{path}.{key}")
                if not valid:
                    return valid, code, child_path
    if isinstance(value, list):
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            return False, "MAX_ITEMS", path
        item_schema = schema.get("items")
        for index, child in enumerate(value):
            valid, code, child_path = _validate_schema_subset(child, item_schema, f"{path}[{index}]")
            if not valid:
                return valid, code, child_path
    return True, None, None


def provider_schema_diagnostics(raw: str, schema: Any) -> dict[str, Any]:
    """Return content-free syntax and provider-schema validation fields."""

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {
            "transport_valid": True,
            "json_syntax_valid": False,
            "provider_schema_valid": False,
            "provider_schema_error_code": "JSON_SYNTAX",
            "provider_schema_error_path": "$",
        }
    valid, code, path = _validate_schema_subset(value, schema)
    return {
        "transport_valid": True,
        "json_syntax_valid": True,
        "provider_schema_valid": valid,
        "provider_schema_error_code": code,
        "provider_schema_error_path": path,
    }


def envelope_error_diagnostics(raw: str, error: Exception, schema: Any) -> dict[str, Any]:
    """Classify a failed response without retaining generated content."""

    result = provider_schema_diagnostics(raw, schema)
    stripped = raw.strip() if isinstance(raw, str) else ""
    message = str(error).casefold()
    if not stripped:
        result.update(error_layer="json_syntax", error_code="EMPTY_OUTPUT", content_class="empty")
    elif not result["json_syntax_valid"]:
        if not stripped.startswith(("{", "[", "```")):
            code, content_class = "PROSE_INSTEAD_OF_JSON", "prose"
        else:
            fingerprint = json_structure_fingerprint(raw)
            incomplete = bool(
                fingerprint.get("json_brace_balance", 0) > 0
                or fingerprint.get("json_bracket_balance", 0) > 0
                or fingerprint.get("json_string_open")
                or fingerprint.get("json_error_at_end")
            )
            code, content_class = (
                ("JSON_TRUNCATED", "container_incomplete") if incomplete
                else ("JSON_MALFORMED", "container_malformed")
            )
        result.update(error_layer="json_syntax", error_code=code, content_class=content_class)
    elif any(marker in message for marker in ("action", "tool")):
        result.update(error_layer="action_contract", error_code="INVALID_ACTION", content_class="valid_json")
    elif result.get("provider_schema_valid") is False:
        result.update(
            error_layer="provider_schema",
            error_code=result.get("provider_schema_error_code") or "SCHEMA_VIOLATION",
            content_class="valid_json",
        )
    else:
        result.update(error_layer="local_contract", error_code="BUSINESS_RULE_VIOLATION", content_class="valid_json")
    result["local_contract_valid"] = False
    result["action_adapter_valid"] = False if result["error_layer"] == "action_contract" else None
    return result


def _unwrap_single_json_fence(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return None
    first_line_end = stripped.find("\n")
    if first_line_end < 0:
        return None
    header = stripped[:first_line_end].strip().casefold()
    if header not in {"```", "```json"}:
        return None
    body = stripped[first_line_end + 1:-3].strip()
    if not body or "```" in body:
        return None
    return body


def _remove_json_trailing_commas(raw: str) -> str | None:
    """Remove commas followed only by whitespace and a container close."""

    output: list[str] = []
    in_string = escaped = False
    changed = False
    for index, char in enumerate(raw):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            output.append(char)
            continue
        if char == ",":
            following = index + 1
            while following < len(raw) and raw[following].isspace():
                following += 1
            if following < len(raw) and raw[following] in "}]":
                changed = True
                continue
        output.append(char)
    return "".join(output) if changed else None


def _escape_json_string_controls(raw: str) -> str | None:
    """Escape JSON-forbidden literal control characters inside strings."""

    replacements = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    output: list[str] = []
    in_string = escaped = False
    changed = False
    for char in raw:
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
                continue
            if char == "\\":
                output.append(char)
                escaped = True
                continue
            if char == '"':
                output.append(char)
                in_string = False
                continue
            if ord(char) < 0x20:
                output.append(replacements.get(char, f"\\u{ord(char):04x}"))
                changed = True
                continue
            output.append(char)
            continue
        output.append(char)
        if char == '"':
            in_string = True
    return "".join(output) if changed else None


def _default_missing_actions(raw: str) -> str | None:
    """Interpret one complete top-level speak object as having no actions."""

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"speak"} or not isinstance(value.get("speak"), dict):
        return None
    return json.dumps({"speak": value["speak"], "actions": []}, ensure_ascii=False, separators=(",", ":"))


def _default_empty_emotion(raw: str) -> str | None:
    """Map an empty emotion slot to the existing neutral local default."""

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"speak", "actions"}:
        return None
    speak = value.get("speak")
    if not isinstance(speak, dict) or set(speak) != {"state", "emotion", "text"} or speak.get("emotion") != "":
        return None
    repaired = dict(value)
    repaired["speak"] = {**speak, "emotion": "calm"}
    return json.dumps(repaired, ensure_ascii=False, separators=(",", ":"))


def parse_compatible_decision_envelope(
    raw: str, *, allow_empty_control_text: bool = False,
) -> CompatibleDecisionEnvelope:
    """Apply only bounded repairs and then enforce the full envelope contract.

    The whole repaired value must pass :class:`SpeechDecisionEnvelopeStream`.
    This function never extracts a brace range, wraps prose, completes a
    truncated container, inserts an ambiguous delimiter, or infers an action.
    """

    if not isinstance(raw, str):
        raise EnvelopeError("decision response must be text")
    queue: list[tuple[str, tuple[str, ...]]] = [(raw, ())]
    seen = {raw}
    transforms = (
        ("utf8_bom", lambda value: value[1:] if value.startswith("\ufeff") else None),
        ("single_json_fence", _unwrap_single_json_fence),
        ("trailing_comma", _remove_json_trailing_commas),
        ("string_control_escape", _escape_json_string_controls),
        ("missing_actions", _default_missing_actions),
        ("empty_emotion", _default_empty_emotion),
    )
    while queue:
        candidate, repairs = queue.pop(0)
        parser = SpeechDecisionEnvelopeStream(allow_empty_control_text=allow_empty_control_text)
        try:
            parser.feed(candidate)
            return CompatibleDecisionEnvelope(parser.finish(), repairs)
        except EnvelopeError:
            pass
        for name, transform in transforms:
            if name in repairs:
                continue
            repaired = transform(candidate)
            if repaired is None or repaired == candidate or repaired in seen:
                continue
            seen.add(repaired)
            queue.append((repaired, (*repairs, name)))
    raise EnvelopeError("decision JSON is not safely recoverable")


def json_structure_fingerprint(raw: str) -> dict[str, Any]:
    """Describe malformed decision JSON without retaining generated text.

    The fingerprint is intentionally limited to syntax and container shape so
    production diagnostics can distinguish prose, fences and truncated JSON
    without logging user-visible model content.
    """

    value = raw if isinstance(raw, str) else ""
    stripped = value.strip()

    def char_class(char: str) -> str:
        if not char:
            return "empty"
        return {
            "{": "object_open", "}": "object_close",
            "[": "array_open", "]": "array_close",
            '"': "quote", "`": "backtick",
        }.get(char, "alpha" if char.isalpha() else "digit" if char.isdigit() else "other")

    braces = brackets = 0
    in_string = escaped = False
    for char in stripped:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1

    result: dict[str, Any] = {
        "json_first": char_class(stripped[:1]),
        "json_last": char_class(stripped[-1:]),
        "json_fenced": stripped.startswith("```") or stripped.endswith("```"),
        "json_brace_balance": braces,
        "json_bracket_balance": brackets,
        "json_string_open": in_string,
    }
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        result["json_syntax_valid"] = False
        if isinstance(exc, json.JSONDecodeError):
            content_end = len(value.rstrip())
            result.update(
                json_error_kind=exc.msg,
                json_error_pos=exc.pos,
                json_error_at_end=exc.pos >= max(0, content_end - 1),
            )
        return result

    result["json_syntax_valid"] = True
    result["json_top_type"] = type(decoded).__name__
    if isinstance(decoded, dict):
        result.update(
            json_has_speak="speak" in decoded,
            json_has_actions="actions" in decoded,
            json_top_key_count=len(decoded),
        )
    return result


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

    def definitive_non_json_prefix(
        self, *, non_whitespace_limit: int = EARLY_NON_JSON_NON_WHITESPACE_LIMIT,
    ) -> bool:
        """Return true once the streamed prefix cannot be an accepted JSON envelope.

        Leading whitespace and the two bounded compatibility wrappers accepted
        at final validation (UTF-8 BOM and one JSON code fence) do not count
        against the limit.  A real decision must then start with ``{``.  The
        method deliberately does not diagnose malformed object JSON; only a
        definitive prose/non-object prefix is terminated early.
        """

        if non_whitespace_limit <= 0:
            raise ValueError("non_whitespace_limit must be positive")
        candidate = self.raw.lstrip()
        if candidate.startswith("\ufeff"):
            candidate = candidate[1:].lstrip()
        if candidate.startswith("{"):
            return False
        if candidate.startswith("```"):
            first_line_end = candidate.find("\n")
            if first_line_end < 0:
                # A supported fence header contains at most seven effective
                # characters (```json).  Once the general threshold is met it
                # can no longer become one of the supported wrappers.
                return sum(not char.isspace() for char in candidate) >= non_whitespace_limit
            header = candidate[:first_line_end].strip().casefold()
            if header not in {"```", "```json"}:
                return sum(not char.isspace() for char in candidate) >= non_whitespace_limit
            candidate = candidate[first_line_end + 1:].lstrip()
            if candidate.startswith("{"):
                return False
        effective_chars = sum(not char.isspace() for char in candidate)
        return effective_chars >= non_whitespace_limit

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
        stream_emotion = self._quoted_value(self.raw, "emotion")
        # Empty emotion has an unambiguous neutral compatibility repair, but
        # finish() remains strict and rejects it unless the compatibility
        # parser rewrites the complete envelope.
        if stream_emotion != "":
            self._emotion(stream_emotion)
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
        raw_emotion = self._quoted_value(self.raw, "emotion")
        emotion = "calm" if raw_emotion == "" else self._emotion(raw_emotion) or "calm"
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
