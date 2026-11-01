"""V0.6（第六版）决策结构：从真实动作合同生成供应方分支，本地验证仍为执行依据。"""
from __future__ import annotations

from typing import Any, Iterable


def decision_schema(action_definitions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build a stable, provider-compatible schema from the local action catalog.

    每个动作项分别匹配其真实参数；供应方结构与本地合同共用动作定义。
    """

    from copy import deepcopy
    definitions = [d for d in action_definitions if isinstance(d, dict) and d.get("name")]
    if any(d["name"] == "request_thinking" for d in definitions):
        raise ValueError("request_thinking（追加思考动作）已归档。")
    branches = [{"type": "object", "additionalProperties": False,
                 "required": ["name", "arguments"], "properties": {
                     "name": {"type": "string", "enum": [d["name"]]},
                     "arguments": deepcopy(d["parameters"]),
                 }} for d in definitions]
    actions = {"type": "array", "maxItems": 8 if branches else 0,
               "items": {"anyOf": branches} if branches else {"type": "string"}}

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
                "required": ["state", "controls", "text"],
                "properties": {
                    # ``revision`` carries only the new tail; the local runtime
                    # stitches it onto the locked prefix.  ``cancel`` may carry
                    # replacement text: empty means stop only, non-empty means
                    # stop and replace.
                    "state": {"type": "string",
                              "enum": ["speak", "revision", "cancel", "wait"]},
                    "controls": {"type": "array", "maxItems": 3, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["name", "value"],
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                    }},
                    "text": {"type": "string"},
                },
            },
            "actions": actions,
        },
    }
