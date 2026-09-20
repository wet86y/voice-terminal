from __future__ import annotations

import time


DEFAULT_SENTENCE_BREAKS = "。！？；，、：….!?;,:\n"


class PunctuationSentenceSegmenter:
    """Split streaming model text at spoken punctuation boundaries.

    The first non-empty fragment has one latency escape hatch: if no
    punctuation arrives within ``first_clause_timeout_ms``, ``flush_due``
    releases the exact text accumulated so far.  Later incomplete text waits
    for punctuation or ``finish``; no character-count split is ever applied.
    """

    min_chars = 1

    def __init__(
        self,
        *,
        breaks: str = DEFAULT_SENTENCE_BREAKS,
        first_clause_timeout_ms: int = 400,
    ) -> None:
        if not breaks:
            raise ValueError("sentence breaks must not be empty")
        if first_clause_timeout_ms <= 0:
            raise ValueError("first_clause_timeout_ms must be positive")
        self.breaks = frozenset(breaks)
        self.max_wait_ms = int(first_clause_timeout_ms)
        self._buffer = ""
        self._started_at: float | None = None
        self._emitted = False

    @property
    def pending_text(self) -> str:
        return self._buffer

    def _take(self) -> str | None:
        value = self._buffer
        if not value or not value.strip():
            return None
        self._buffer = ""
        self._started_at = None
        self._emitted = True
        return value

    def feed(self, delta: str, *, now: float | None = None) -> list[str]:
        if not delta:
            return []
        current = time.monotonic() if now is None else now
        if self._started_at is None and not self._emitted and (self._buffer + delta).strip():
            self._started_at = current
        result: list[str] = []
        index = 0
        while index < len(delta):
            char = delta[index]
            self._buffer += char
            if char in self.breaks:
                # Keep adjacent punctuation delivered in the same model delta
                # on the preceding clause (for example ``？！`` or ``……``).
                while index + 1 < len(delta) and delta[index + 1] in self.breaks:
                    index += 1
                    self._buffer += delta[index]
                value = self._take()
                if value is not None:
                    result.append(value)
            index += 1
        return result

    def flush_due(self, *, now: float | None = None) -> list[str]:
        if self._emitted or self._started_at is None or not self._buffer.strip():
            return []
        current = time.monotonic() if now is None else now
        if (current - self._started_at) * 1000 < self.max_wait_ms:
            return []
        value = self._take()
        return [value] if value is not None else []

    def flush_wait_seconds(self, *, now: float | None = None) -> float | None:
        """Return the remaining one-shot first-clause deadline."""

        if self._emitted or self._started_at is None or not self._buffer.strip():
            return None
        current = time.monotonic() if now is None else now
        elapsed = current - self._started_at
        return max(0.0, self.max_wait_ms / 1000 - elapsed)

    def finish(self) -> list[str]:
        if not self._buffer.strip():
            self._buffer = ""
            self._started_at = None
            return []
        value = self._take()
        return [value] if value is not None else []


def split_complete_text(
    text: str,
    *,
    breaks: str = DEFAULT_SENTENCE_BREAKS,
) -> list[str]:
    """Split a completed replacement suffix without a latency timer."""

    segmenter = PunctuationSentenceSegmenter(breaks=breaks)
    result = segmenter.feed(text)
    result.extend(segmenter.finish())
    return result
