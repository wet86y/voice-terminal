from __future__ import annotations

from datetime import datetime


class InputManager:
    def __init__(self) -> None:
        self._counter = 0
        self._current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    def begin(self) -> str:
        self._counter += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._current = f"input_{stamp}_{self._counter:06d}"
        return self._current

    def invalidate(self) -> str | None:
        old = self._current
        self._current = None
        return old

    def is_current(self, turn_id: str | None) -> bool:
        return bool(turn_id and turn_id == self._current)
