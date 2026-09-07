from __future__ import annotations

import json


class ToolCallAssembler:
    """Assemble native streaming tool-call deltas for AgentEpisodeRuntime."""

    def __init__(self) -> None:
        self.calls: dict[int, dict] = {}

    def feed(self, value: dict) -> None:
        index = value.get("index")
        if type(index) is not int or not 0 <= index < 8:
            raise ValueError("invalid tool call index")
        call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if value.get("id"):
            call["id"] += value["id"]
        function = value.get("function") or {}
        for key in ("name", "arguments"):
            if function.get(key):
                if not isinstance(function[key], str):
                    raise ValueError("invalid tool delta")
                call["function"][key] += function[key]
                if len(call["function"][key]) > 16384:
                    raise ValueError("tool parameters too large")

    def complete(self) -> list[dict]:
        result = [self.calls[key] for key in sorted(self.calls)]
        ids: set[str] = set()
        for call in result:
            if not call["id"] or call["id"] in ids or not call["function"]["name"]:
                raise ValueError("incomplete tool call")
            ids.add(call["id"])
            if not isinstance(json.loads(call["function"]["arguments"]), dict):
                raise ValueError("tool arguments must be an object")
        return result
