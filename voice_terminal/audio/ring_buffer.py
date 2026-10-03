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
        self._snapshot: tuple[int | None, bytes] | None = None
        from ..resources import registry, ResourcePolicy
        self._reuse_snapshots = ResourcePolicy.load().shared_audio
        registry.observe(self, "capture_ring", lambda value: value.managed_bytes)

    @property
    def managed_bytes(self) -> int:
        with self._lock:
            return self._size + (len(self._snapshot[1]) if self._snapshot else 0)

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._size

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._snapshot = None
            self._chunks.append(bytes(chunk))
            self._size += len(chunk)
            while self._size > self.max_bytes and self._chunks:
                self._size -= len(self._chunks.popleft())

    def read(self, max_bytes: int | None = None) -> bytes:
        with self._lock:
            if not self._reuse_snapshots:
                data = b"".join(self._chunks)
                return data if max_bytes is None or len(data) <= max_bytes else data[-max_bytes:]
            if self._snapshot is not None and self._snapshot[0] == max_bytes:
                return self._snapshot[1]
            if max_bytes is None or max_bytes <= 0 or self._size <= max_bytes:
                data = b"".join(self._chunks)
                if max_bytes is not None and len(data) > max_bytes:
                    data = data[-max_bytes:]  # 保持原零及负数边界的切片语义。
            else:
                selected, remaining = [], max_bytes
                for chunk in reversed(self._chunks):
                    if remaining <= 0:
                        break
                    selected.append(chunk[-remaining:])
                    remaining -= len(selected[-1])
                data = b"".join(reversed(selected))
            self._snapshot = (max_bytes, data)
            return data

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._snapshot = None
