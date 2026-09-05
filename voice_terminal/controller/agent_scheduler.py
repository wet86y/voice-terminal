from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class PredictionScheduler:
    """One runner and one replaceable snapshot. Finalization never awaits it."""

    def __init__(self, run: Callable[[dict], Awaitable[None]], interval_ms: int = 800) -> None:
        self.run = run
        self.interval = interval_ms / 1000
        self.pending: dict | None = None
        self.task: asyncio.Task | None = None
        self.last_start = 0.0
        self.closed = False

    def submit(self, snapshot: dict) -> None:
        if self.closed:
            return
        self.pending = snapshot
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self.pending is not None and not self.closed:
            await asyncio.sleep(max(0, self.last_start + self.interval - time.monotonic()))
            if self.closed:
                return
            snapshot, self.pending = self.pending, None
            self.last_start = time.monotonic()
            try:
                await self.run(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Prediction is optional; final requests use a separate task.
                continue

    def stop(self) -> asyncio.Task | None:
        self.closed = True
        self.pending = None
        if self.task and not self.task.done():
            self.task.cancel()
        return self.task
