from __future__ import annotations

from collections import deque
from threading import Lock


class PcmRingBuffer:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = Lock()

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._size

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._chunks.append(bytes(chunk))
            self._size += len(chunk)
            while self._size > self.max_bytes and self._chunks:
                self._size -= len(self._chunks.popleft())

    def read(self, max_bytes: int | None = None) -> bytes:
        with self._lock:
            data = b"".join(self._chunks)
        if max_bytes is None or len(data) <= max_bytes:
            return data
        return data[-max_bytes:]

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
