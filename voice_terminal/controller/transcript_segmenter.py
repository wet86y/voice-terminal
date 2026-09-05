from __future__ import annotations

from os.path import commonprefix


class TranscriptSegmenter:
    def __init__(self, min_chars: int = 6, max_chars: int = 20) -> None:
        self.min_chars, self.max_chars = min_chars, max_chars
        self.previous = ""
        self.emitted = ""
        self.segments: list[str] = []

    def feed(self, text: str, *, pause: bool = False) -> dict | None:
        stable = commonprefix([self.previous, text])
        corrected = not text.startswith(self.emitted)
        if corrected:
            prefix = commonprefix([self.emitted, text])
            kept: list[str] = []
            for segment in self.segments:
                if len("".join(kept)) + len(segment) > len(prefix):
                    break
                kept.append(segment)
            self.segments = kept
            self.emitted = "".join(kept)
        self.previous = text
        remaining = stable[len(self.emitted):]
        additions: list[str] = []
        while len(remaining) >= self.min_chars:
            limit = min(len(remaining), self.max_chars)
            breaks = [i + 1 for i, c in enumerate(remaining[:limit]) if c in "，。！？；,!?;" and i + 1 >= self.min_chars]
            length = breaks[0] if breaks else (limit if len(remaining) >= self.max_chars or pause else 0)
            if not length:
                break
            additions.append(remaining[:length])
            self.segments.append(remaining[:length])
            self.emitted += remaining[:length]
            remaining = remaining[length:]
        if not additions and not corrected:
            return None
        return {"text": text, "new_segment": "".join(additions), "corrected": corrected,
                "segments": list(self.segments)}
