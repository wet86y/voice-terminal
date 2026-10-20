"""Runtime operations（运行操作）：跨组件共用、可随快照恢复的状态契约。"""
from __future__ import annotations

from uuid import uuid4


class OperationRegistry:
    """调用方负责同步；只保留有限终态，活动操作始终保留。"""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def begin(self, target: str, label: str, phase: str = "switching") -> str:
        key = uuid4().hex
        self.items[key] = {"id": key, "revision": 1, "target": target, "label": label,
                           "state": "running", "phase": phase, "detail": ""}
        self._prune()
        return key

    def update(self, key: str, **values):
        row = self.items[key]
        row.update(values, revision=row["revision"] + 1)

    def snapshot(self) -> list[dict]:
        return [dict(row) for row in self.items.values()]

    def _prune(self):
        terminal = [key for key, row in self.items.items() if row["state"] != "running"]
        for key in terminal[:-16]:
            del self.items[key]
