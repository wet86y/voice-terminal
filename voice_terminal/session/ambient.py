from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
from typing import Any, Mapping

from ..core.events import utc_now_iso


class AmbientTranscriptStore:
    """AmbientTranscriptStore（环境文字区）：保存本会话未获身份准入的有界转写。"""

    def __init__(self, *, max_records: int = 100, max_bytes: int = 64 * 1024) -> None:
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1024, int(max_bytes))
        self._records: deque[dict[str, Any]] = deque()
        self._bytes = 0

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(item) for item in self._records)

    def append(self, value: Mapping[str, Any]) -> dict[str, Any]:
        text = str(value.get("text") or "").strip()
        if not text:
            return {}
        raw_similarity = value.get("similarity")
        try:
            normalized_similarity = (
                float(raw_similarity)
                if isinstance(raw_similarity, (int, float)) and not isinstance(raw_similarity, bool)
                else None
            )
        except (OverflowError, TypeError, ValueError):
            normalized_similarity = None
        if normalized_similarity is not None and not math.isfinite(normalized_similarity):
            normalized_similarity = None
        item: dict[str, Any] = {
            "timestamp": str(value.get("timestamp") or utc_now_iso())[:80],
            "turn_id": str(value.get("turn_id") or "")[:160],
            "text": text[:4000],
            "speaker_state": str(value.get("speaker_state") or "non_owner_confirmed")[:80],
            "similarity": normalized_similarity,
            "reason": str(value.get("reason") or "")[:240],
            "route": "ambient",
            "refined_text": str(value.get("refined_text") or "")[:4000],
        }
        size = len(str(item).encode("utf-8"))
        if size > self.max_bytes:
            # Keep the bounded record useful even with a deliberately tiny
            # test capacity: prefer the primary transcript, then discard the
            # optional refinement/diagnostic text until the serialized item
            # itself fits the byte budget.
            item["refined_text"] = ""
            item["reason"] = ""
            item["text"] = item["text"][: max(1, self.max_bytes // 4)]
            size = len(str(item).encode("utf-8"))
        while size > self.max_bytes and item["text"]:
            item["text"] = item["text"][: max(1, len(item["text"]) // 2)]
            size = len(str(item).encode("utf-8"))
        if size > self.max_bytes:
            item["text"] = ""
            item["refined_text"] = ""
            item["similarity"] = None
            size = len(str(item).encode("utf-8"))
        while self._records and (len(self._records) >= self.max_records or self._bytes + size > self.max_bytes):
            removed = self._records.popleft()
            self._bytes -= len(str(removed).encode("utf-8"))
        self._records.append(item)
        self._bytes += size
        return deepcopy(item)

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(50, int(limit)))
        values = list(self._records)[-limit:]
        return {
            "records": deepcopy(values),
            "count": len(values),
            "total_count": len(self._records),
            "truncated": len(values) < len(self._records),
            "session_scoped": True,
            "generated_at": utc_now_iso(),
        }

    def update_refined(self, turn_id: str, refined_text: str) -> bool:
        """Attach a late SenseVoice refinement to an existing ambient item."""
        turn_id = str(turn_id or "")
        refined_text = str(refined_text or "").strip()
        if not turn_id or not refined_text:
            return False
        for item in self._records:
            if item.get("turn_id") != turn_id:
                continue
            item["refined_text"] = refined_text[:4000]
            self._bytes = sum(len(str(value).encode("utf-8")) for value in self._records)
            while self._records and self._bytes > self.max_bytes:
                removed = self._records.popleft()
                self._bytes -= len(str(removed).encode("utf-8"))
            return True
        return False

    def clear(self) -> None:
        self._records.clear()
        self._bytes = 0
