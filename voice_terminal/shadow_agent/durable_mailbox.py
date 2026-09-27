"""Durable DSH-to-Voice event mailbox（持久化影子到语音事件信箱）."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from uuid import uuid4

from .contracts import ShadowEvent


class DurableShadowMailbox:
    """Persist accepted events before acknowledging their DSH publisher."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("Voice 信箱文件格式无效。")
        self._pending = dict(document.get("pending") or {})
        self._delivered = list(document.get("delivered") or [])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1,
            "pending": self._pending,
            "delivered": self._delivered[-4096:],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    async def publish(self, event: ShadowEvent) -> str:
        async with self._lock:
            if event.event_id in self._delivered:
                return "duplicate"
            current = self._pending.get(event.event_id)
            value = asdict(event)
            if current is not None:
                return "duplicate" if current == value else "conflict"
            self._pending[event.event_id] = value
            self._save()
            return "accepted"

    async def pending(self) -> list[ShadowEvent]:
        async with self._lock:
            return [ShadowEvent(**value) for value in self._pending.values()]

    async def acknowledge(self, event_id: str) -> None:
        async with self._lock:
            if self._pending.pop(event_id, None) is not None:
                self._delivered.append(event_id)
                self._delivered = self._delivered[-4096:]
                self._save()
