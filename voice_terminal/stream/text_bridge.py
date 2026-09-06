from __future__ import annotations

import time


class TextBridge:
    """Turn LLM deltas into short, speakable text segments."""

    def __init__(
        self,
        *,
        min_chars: int = 8,
        max_chars: int = 36,
        max_wait_ms: int = 350,
        preferred_breaks: str = "。！？；，、",
        clock=time.monotonic,
    ) -> None:
        if min_chars <= 0 or max_chars < min_chars or max_wait_ms <= 0:
            raise ValueError("文本桥接参数无效")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.max_wait_ms = max_wait_ms
        self.preferred_breaks = preferred_breaks
        self._clock = clock
        self._buffer = ""
        self._started_at: float | None = None

    @property
    def buffer(self) -> str:
        return self._buffer

    def reset(self) -> None:
        self._buffer = ""
        self._started_at = None

    def _flush_at(self, index: int) -> str:
        value = self._buffer[:index].strip()
        self._buffer = self._buffer[index:]
        if self._buffer.strip():
            self._started_at = self._clock()
        else:
            self._started_at = None
        return value

    def _break_index(self, limit: int) -> int | None:
        # ``min_chars`` is a lower bound for normal streaming chunks.  The old
        # implementation considered a break at position 1–7 as soon as the
        # *whole* buffer reached eight characters, which emitted fragments such
        # as ``好的，`` despite ``min_chars=8``.  Those tiny frames are both
        # unnatural for TTS and unnecessarily increase the provider message
        # burst.  A short tail is still allowed through ``finish()``.
        candidates = [
            index + 1
            for index, char in enumerate(self._buffer[:limit])
            if char in self.preferred_breaks and index + 1 >= self.min_chars
        ]
        if candidates:
            return max(candidates)
        whitespace = [
            index + 1
            for index, char in enumerate(self._buffer[:limit])
            if char.isspace() and index + 1 >= self.min_chars
        ]
        return max(whitespace) if whitespace else None

    def feed(self, text_delta: str) -> list[str]:
        if not text_delta:
            return []
        if self._started_at is None:
            self._started_at = self._clock()
        self._buffer += text_delta
        result: list[str] = []

        while len(self._buffer) >= self.min_chars:
            break_index = self._break_index(self.max_chars)
            if break_index is not None:
                result.append(self._flush_at(break_index))
                continue
            if len(self._buffer) >= self.max_chars:
                result.append(self._flush_at(self.max_chars))
                continue
            break

        elapsed_ms = ((self._clock() - self._started_at) * 1000) if self._started_at is not None else 0
        if self._buffer and elapsed_ms >= self.max_wait_ms and len(self._buffer) >= self.min_chars:
            result.append(self._flush_at(len(self._buffer)))
        return [item for item in result if item]

    def finish(self) -> list[str]:
        if not self._buffer.strip():
            self.reset()
            return []
        value = self._buffer.strip()
        self.reset()
        return [value]

    def flush_due(self, *, now: float | None = None) -> list[str]:
        """Flush a normal-sized buffer once its time window has elapsed.

        The controller calls this before feeding each new LLM delta.  Keeping
        the method on the legacy bridge lets the Bidi and legacy paths share
        the same stream boundary without changing the old minimum-size rule:
        a short tail remains buffered until ``finish()``.
        """

        if not self._buffer or self._started_at is None or len(self._buffer) < self.min_chars:
            return []
        current = self._clock() if now is None else now
        if (current - self._started_at) * 1000 < self.max_wait_ms:
            return []
        return [self._flush_at(len(self._buffer))]
