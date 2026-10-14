from __future__ import annotations

from ..agent.sentences import SentenceStore


class TranscriptSegmenter:
    def __init__(self, min_chars: int = 6, max_chars: int = 20, *, boundary_adjuster=None) -> None:
        self.min_chars, self.max_chars = min_chars, max_chars
        self.store = SentenceStore(min_chars, max_chars, boundary_adjuster=boundary_adjuster)

    def feed(self, text: str, *, pause: bool = False) -> dict | None:
        result = self.store.feed(text, pause=pause)
        if result is None:
            return None
        # Preserve the legacy strings while exposing revisioned records to
        # the new scheduler/controller path.
        result["segments"] = [item["text"] for item in result["sentences"]]
        return result
