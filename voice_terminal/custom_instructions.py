"""Validated user instruction entries and their model-specific projections."""

from __future__ import annotations

import json
import re
from typing import Any


MAX_ENTRIES = 50
MAX_TOTAL_BYTES = 32 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FIELDS = {"id", "trigger", "target", "instruction", "enabled"}


def validate_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise ValueError("自定义指令必须是最多 50 条的列表。")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or not _FIELDS <= set(raw) <= _FIELDS | {"keywords", "name"}:
            raise ValueError("自定义指令须包含 id（标识）、trigger（触发语义）、target（执行位置）、instruction（目标）、enabled（启用状态），可选 keywords（关键词列表）、name（显示名称）。")
        identifier = raw["id"]
        trigger = raw["trigger"]
        instruction = raw["instruction"]
        target = raw["target"]
        name = raw.get("name", "")
        if not isinstance(name, str) or len(name.strip()) > 80 or "\x00" in name:
            raise ValueError("指令名称须为最多 80 字的文本，可留空。")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen:
            raise ValueError("自定义指令 id（标识）无效或重复。")
        if (not isinstance(trigger, str) or not trigger.strip() or len(trigger) > 200
                or "\x00" in trigger):
            raise ValueError("触发语义须为 1～200 字且不能包含 NUL。")
        if (not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000
                or "\x00" in instruction):
            raise ValueError("执行目标须为 1～2000 字且不能包含 NUL。")
        if not isinstance(target, str) or target not in {"voice", "shadow"} or type(raw["enabled"]) is not bool:
            raise ValueError("执行位置或启用状态无效。")
        keywords = raw.get("keywords", [])
        if not isinstance(keywords, list) or any(
            not isinstance(word, str) or not re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,4}", word.strip())
            for word in keywords
        ):
            raise ValueError("触发关键词须为列表，每项为 2～4 个汉字。")
        seen.add(identifier)
        entries.append({
            "id": identifier, "trigger": trigger.strip(), "target": target,
            "instruction": instruction.strip(), "enabled": raw["enabled"],
            **({"name": name.strip()} if "name" in raw else {}),
            # 缺字段的旧条目按空列表使用，保留旧快照形状以兼容已有会话。
            **({"keywords": list(dict.fromkeys(word.strip() for word in keywords))}
               if "keywords" in raw else {}),
        })
    if len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > MAX_TOTAL_BYTES:
        raise ValueError("自定义指令总大小不得超过 32 KB。")
    return entries


def voice_projection(entries: list[dict[str, Any]]) -> str:
    rows = [
        {"id": item["id"], "触发语义": item["trigger"],
         "执行位置": "影子代理" if item["target"] == "shadow" else "语音链路",
         **({"执行目标": item["instruction"]} if item["target"] == "voice" else {})}
        for item in entries if item["enabled"]
    ]
    return json.dumps(rows, ensure_ascii=False) if rows else ""


def shadow_projection(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"id": item["id"], "触发语义": item["trigger"], "执行目标": item["instruction"]}
        for item in entries if item["enabled"] and item["target"] == "shadow"
    ]
