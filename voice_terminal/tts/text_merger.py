from __future__ import annotations

import time


class BidiTextMerger:
    """Small time/size bounded merger for a provider with server-side breaks.

    MiniMax Bidi performs sentence buffering itself.  The client therefore
    sends short, ordered text frames instead of waiting for a local sentence
    boundary.  Punctuation is preserved byte-for-byte; it is never inserted
    or removed to make a frame reach the size limit.
    """

    def __init__(self, *, max_chars: int = 128, max_wait_ms: int = 30) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if max_wait_ms <= 0:
            raise ValueError("max_wait_ms must be positive")
        self.max_chars = max_chars
        self.max_wait_ms = max_wait_ms
        self._parts: list[str] = []
        self._started_at: float | None = None

    @property
    def pending_text(self) -> str:
        return "".join(self._parts)

    def feed(self, delta: str, *, now: float | None = None) -> list[str]:
        if not delta:
            return []
        current = time.monotonic() if now is None else now
        result: list[str] = []
        remainder = delta
        split_due_to_size = False
        # A single LLM callback can contain more than max_chars.  Split that
        # callback into bounded frames while preserving every code point and
        # the punctuation supplied by the model.  The common case still
        # produces one frame at the first size/time boundary.
        while remainder:
            if self._started_at is None:
                self._started_at = current
            capacity = self.max_chars - len(self.pending_text)
            if capacity <= 0:
                result.extend(self._flush())
                continue
            piece, remainder = remainder[:capacity], remainder[capacity:]
            self._parts.append(piece)
            elapsed = (current - self._started_at) * 1000
            if len(self.pending_text) >= self.max_chars or (not remainder and elapsed >= self.max_wait_ms):
                result.extend(self._flush())
                if remainder:
                    split_due_to_size = True
        # A single provider/LLM callback that exceeds the frame limit is
        # already available in full.  Emit its short final tail immediately
        # instead of leaving it stranded until a later callback or the 30 ms
        # timer; ordinary small callbacks still use the time window above.
        if split_due_to_size and self._parts:
            result.extend(self._flush())
        return result

    def finish(self) -> list[str]:
        return self._flush()

    def flush_due(self, *, now: float | None = None) -> list[str]:
        """Release a pending frame whose 30 ms window has elapsed.

        The merger is intentionally synchronous and does not own an asyncio
        timer.  A controller can call this at stream boundaries (or from a
        lightweight ticker) so a stalled upstream producer cannot keep an
        already complete prefix buffered indefinitely.
        """

        if self._started_at is None:
            return []
        current = time.monotonic() if now is None else now
        if (current - self._started_at) * 1000 >= self.max_wait_ms:
            return self._flush()
        return []

    def _flush(self) -> list[str]:
        if not self._parts:
            return []
        value = "".join(self._parts)
        self._parts.clear()
        self._started_at = None
        return [value]
