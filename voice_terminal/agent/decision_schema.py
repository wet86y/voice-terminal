"""Provider-facing JSON Schema for the V0.6 decision envelope.

The schema deliberately describes only the subset accepted by DeepSeek's
Responses implementation.  Conditional speech rules and action-specific
argument validation remain authoritative in the local runtime.
"""
from __future__ import annotations

from typing import Any, Iterable


EMOTIONS = ["calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"]


def decision_schema(action_definitions: Iterable[dict[str, Any]]) -> dict[str, Any]:
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
                    # action in this provider subset. Preserve a shared const
                    # (notably the content-free wake boolean); conflicting
                    # action shapes fall back to local authoritative checks.
                    value_type = value.get("type")
                    projected = {"type": value_type if isinstance(value_type, str) else "string"}
                    if "const" in value:
                        projected["const"] = value["const"]
                    existing = argument_properties.get(key)
                    if existing is None:
                        argument_properties[key] = projected
                    elif existing != projected:
                        # Merged actions must share a neutral shape. A local
                        # parser still enforces any action-specific constraint.
                        existing.pop("const", None)

    if "request_thinking" in names:
        raise ValueError("request_thinking（追加思考动作）已归档，不能用于当前动作目录。")

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
                # Responses providers require required to match properties.
                # Irrelevant merged fields are normalized away locally.
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
                    # ``revision`` carries only the new tail; the local runtime
                    # stitches it onto the locked prefix.  ``cancel`` may carry
                    # replacement text: empty means stop only, non-empty means
                    # stop and replace.
                    "state": {"type": "string",
                              "enum": ["speak", "revision", "cancel", "wait"]},
                    "emotion": {"type": "string", "enum": EMOTIONS},
                    "text": {"type": "string"},
                },
            },
            "actions": actions,
        },
    }
