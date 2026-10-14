"""Stable, revisioned sentence records for the realtime voice scheduler."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from os.path import commonprefix
import time


_BREAKS = "，。！？；,!?;"


@dataclass
class Sentence:
    sentence_id: int
    revision: int
    text: str
    state: str
    created_at: float
    updated_at: float

    def snapshot(self) -> dict:
        return asdict(self)


class SentenceStore:
    """Converts a mutable ASR prefix into stable sentence revisions.

    Only the open tail may be revised.  Once a sentence is sealed, a later
    ASR regression never renumbers preceding sentences; uncertain refinements
    are represented separately by the scheduler instead of rewriting history.
    """
    def __init__(self, min_chars: int = 6, max_chars: int = 20, *, boundary_adjuster=None) -> None:
        self.min_chars, self.max_chars = min_chars, max_chars
        self.boundary_adjuster = boundary_adjuster  # boundary_adjuster（仅长度强制切分时的热词边界保护）。
        self.previous = ""
        self.emitted = ""
        self._next_id = 1
        self.sentences: list[Sentence] = []

    def _append(self, text: str, state: str, now: float) -> Sentence:
        item = Sentence(self._next_id, 1, text, state, now, now)
        self._next_id += 1
        self.sentences.append(item)
        self.emitted += text
        return item

    def feed(self, text: str, *, pause: bool = False, final: bool = False,
             now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        # A monotonic ASR extension is stable through its newly completed
        # punctuation.  Regressions use only the common prefix so they cannot
        # renumber already sealed sentences.
        stable = text if not self.previous or text.startswith(self.previous) else commonprefix([self.previous, text])
        self.previous = text
        # We deliberately do not delete sealed records.  If ASR no longer
        # agrees with the sealed prefix, surface that as a correction signal;
        # preserving identifiers is more valuable than pretending the old
        # sentence never existed.
        corrected = not text.startswith(self.emitted)
        remaining = stable[len(self.emitted):] if text.startswith(self.emitted) else ""
        added: list[Sentence] = []
        while remaining:
            limit = min(len(remaining), self.max_chars)
            breaks = [i + 1 for i, c in enumerate(remaining[:limit])
                      if c in _BREAKS and i + 1 >= self.min_chars]
            length = breaks[0] if breaks else 0
            if not length and len(remaining) >= self.max_chars:
                length = self.boundary_adjuster(remaining, limit) if self.boundary_adjuster else limit
            if not length and (pause or final) and len(remaining) >= self.min_chars:
                length = len(remaining)
            if not length:
                break
            state = "final" if final and length == len(remaining) else "sealed"
            added.append(self._append(remaining[:length], state, now))
            remaining = remaining[length:]
        if final and text.startswith(self.emitted):
            tail = text[len(self.emitted):]
            if tail.strip():
                added.append(self._append(tail, "final", now))
        if not added and not corrected:
            return None
        return {
            "text": text,
            "corrected": corrected,
            "new_segment": "".join(item.text for item in added),
            "sentences": [item.snapshot() for item in self.sentences],
            "changed_ids": [item.sentence_id for item in added],
        }

    @staticmethod
    def _normalise(value: str) -> str:
        return "".join(char for char in value if not char.isspace() and char not in _BREAKS)

    def map_refinement(self, text: str) -> dict:
        """Return a conservative sentence-level refinement projection.

        Matching is intentionally simple: whole normalised sentence anchors
        must occur in the refined text in order.  Ambiguous or absent anchors
        retain the full refinement as a model-visible fallback.
        """
        refined = self._normalise(text)
        cursor = 0
        matched: list[int] = []
        for item in self.sentences:
            anchor = self._normalise(item.text)
            if not anchor:
                continue
            index = refined.find(anchor, cursor)
            if index < 0:
                continue
            matched.append(item.sentence_id)
            cursor = index + len(anchor)
        confidence = len(matched) / len(self.sentences) if self.sentences else 0.0
        return {
            "sentence_ids": matched,
            "confidence": confidence,
            "unmapped_refinement": "" if confidence >= 0.75 else text,
        }


def map_refinement(sentences: list[dict], text: str) -> dict:
    """Map a refinement against serialized sentence snapshots."""
    store = SentenceStore()
    now = time.time()
    for row in sentences:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        item = Sentence(
            int(row.get("sentence_id") or store._next_id),
            int(row.get("revision") or 1), str(row["text"]),
            str(row.get("state") or "sealed"), now, now,
        )
        store.sentences.append(item)
        store._next_id = max(store._next_id, item.sentence_id + 1)
    return store.map_refinement(text)
