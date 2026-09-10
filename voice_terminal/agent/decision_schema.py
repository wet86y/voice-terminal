"""Provider-facing JSON Schema for the V0.6 decision envelope.

The schema deliberately describes only the subset accepted by DeepSeek's
Responses implementation.  Conditional speech rules and action-specific
argument validation remain authoritative in the local runtime.
"""
from __future__ import annotations

from typing import Any, Iterable


EMOTIONS = ["calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"]
THINKING_ACTION = "request_thinking"


def decision_schema(action_definitions: Iterable[dict[str, Any]], *, allow_thinking: bool = False) -> dict[str, Any]:
    """Build a stable, provider-compatible schema from the local action catalog.

    DeepSeek currently rejects the discriminated unions which would normally
    express the state/text and action/arguments relationships.  We therefore
    merge known argument properties into one closed object and retain local
    validation for the conditional parts.
    """

    names: list[str] = []
    argument_properties: dict[str, Any] = {}
    for definition in action_definitions:
        if not isinstance(definition, dict):
            continue
        name = definition.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
        parameters = definition.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict):
            for key, value in properties.items():
                if isinstance(key, str) and isinstance(value, dict):
                    # Every merged argument property is required for every
                    # action in this provider subset. Keep only its base type
                    # so irrelevant fields can carry a neutral value; the
                    # action adapter remains authoritative for constraints.
                    value_type = value.get("type")
                    argument_properties.setdefault(
                        key, {"type": value_type if isinstance(value_type, str) else "string"},
                    )

    if THINKING_ACTION in names:
        raise ValueError(f"{THINKING_ACTION} is reserved for the local Agent control plane")
    if allow_thinking:
        names.append(THINKING_ACTION)
        argument_properties["effort"] = {"type": "string"}

    # JSON Schema requires a non-empty enum.  With no actions available, an
    # impossible sentinel plus maxItems=0 keeps the public envelope stable.
    action_names = names or ["__no_actions_available__"]
    action_item: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "arguments"],
        "properties": {
            "name": {"type": "string", "enum": action_names},
            "arguments": {
                "type": "object",
                "additionalProperties": False,
                "properties": argument_properties,
                "required": list(argument_properties),
            },
        },
    }
    actions: dict[str, Any] = {"type": "array", "maxItems": 8, "items": action_item}
    if not names:
        actions["maxItems"] = 0
        # The compiler still requires an items schema, while an object item
        # with no properties is rejected by some provider revisions.
        actions["items"] = {"type": "string"}

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["speak", "actions"],
        "properties": {
            "speak": {
                "type": "object",
                "additionalProperties": False,
                # DeepSeek strict schemas require every declared property to
                # be listed in required. Empty text represents wait/cancel at
                # this provider boundary and is normalized locally.
                "required": ["state", "emotion", "text"],
                "properties": {
                    "state": {"type": "string", "enum": ["speak", "cancel", "wait"]},
                    "emotion": {"type": "string", "enum": EMOTIONS},
                    "text": {"type": "string"},
                },
            },
            "actions": actions,
        },
    }
