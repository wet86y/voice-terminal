"""跨窗口衔接文档（continuity document）和静默会话整理器。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

CONTINUITY_MAX_CHARS = 6000
CONTINUITY_IDLE_SECONDS = 60 * 60


class ContinuityManager:
    """维护一份短文档，并在静默后异步替换。"""

    def __init__(
        self,
        path: Path,
        summarize: Callable[[Sequence[dict], str], Awaitable[Any]],
        *,
        on_document_changed: Callable[[str], None] | None = None,
        on_organized: Callable[[Any, int], Awaitable[bool]] | None = None,
        on_failure: Callable[[Exception], Awaitable[None]] | None = None,
        idle_seconds: float = CONTINUITY_IDLE_SECONDS,
        max_document_chars: int = CONTINUITY_MAX_CHARS,
        retry_seconds: float = 60,
    ) -> None:
        self.path = path
        self.summarize = summarize
        self.on_document_changed = on_document_changed
        self.on_organized = on_organized
        self.on_failure = on_failure
        self.retry_seconds = max(.01, retry_seconds)
        self.idle_seconds = max(0.0, float(idle_seconds))
        self.max_document_chars = max_document_chars
        self.document = self._read_document()
        self._generation = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._organizer_task: asyncio.Task[Any] | None = None
        self._snapshot_factory: Callable[[], Sequence[dict]] | None = None
        self._captured_snapshot: list[dict] | None = None

    def _read_document(self) -> str:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except (OSError, ValueError) as exc:
            logger.warning("continuity.document_read_failed error=%s", type(exc).__name__)
            return ""
        text = value.get("document") if isinstance(value, dict) else ""
        return text if isinstance(text, str) else ""

    def _bounded(self, value: str) -> str:
        text = "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()
        if len(text) > self.max_document_chars:
            raise ValueError("交接文档超限，保留原文档等待压缩重试。")
        return text

    def user_started(self) -> None:
        """Invalidate any organizer as soon as a new user session begins."""

        self._generation += 1
        for task in (self._idle_task, self._organizer_task):
            if task is not None and not task.done():
                task.cancel()
        self._idle_task = None
        self._organizer_task = None
        self._snapshot_factory = None
        self._captured_snapshot = None

    def arm(
        self,
        anchor_wall_time: float,
        snapshot_factory: Callable[[], Sequence[dict]],
    ) -> None:
        """Start the one-hour timer from the finalized KWS silence anchor."""

        self.user_started()
        self._snapshot_factory = snapshot_factory
        generation = self._generation
        self._idle_task = asyncio.create_task(
            self._wait_then_organize(generation, float(anchor_wall_time)),
            name="continuity-idle-organizer",
        )

    def capture_before_reset(self, snapshot: Sequence[dict]) -> None:
        """Preserve the in-memory window before SessionLedger.reset()."""

        self._captured_snapshot = [dict(item) for item in snapshot if isinstance(item, dict)]

    async def _wait_then_organize(self, generation: int, anchor_wall_time: float) -> None:
        delay = max(0.0, anchor_wall_time + self.idle_seconds - time.time())
        retrying = False
        try:
            await asyncio.sleep(delay)
            if generation != self._generation:
                return
            snapshot = self._captured_snapshot
            if snapshot is None:
                factory = self._snapshot_factory
                snapshot = [dict(item) for item in factory()] if factory is not None else []
            previous = self.document
            organizer = asyncio.create_task(
                self.summarize(snapshot, previous), name="continuity-shadow-summary",
            )
            self._organizer_task = organizer
            updated = await organizer
            if generation != self._generation:
                return
            bounded = self._bounded(updated) if isinstance(updated, str) else updated
            if self.on_organized is None:
                self._replace_document(bounded)
            elif not await self.on_organized(bounded, generation):
                return
            logger.info(
                "continuity.document_replaced chars=%d snapshot_items=%d",
                len(self.document), len(snapshot),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A provider failure is not an empty result: retain the current
            # document and allow the next idle window to retry.
            logger.warning("continuity.organizer_failed error=%s", type(exc).__name__)
            if self.on_failure is not None:
                await self.on_failure(exc)
            if generation == self._generation:
                retrying = True
                self._idle_task = asyncio.create_task(self._wait_then_organize(
                    generation, time.time() + self.retry_seconds - self.idle_seconds),
                    name="continuity-organizer-retry")
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None
            if self._organizer_task is not None and self._organizer_task.done():
                self._organizer_task = None
            if generation == self._generation and not retrying:
                self._snapshot_factory = None
                self._captured_snapshot = None

    def _replace_document(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps({"schema_version": 1, "document": text}, ensure_ascii=False, indent=2)
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.document = text
        if self.on_document_changed is not None:
            self.on_document_changed(text)

    def commit_document(self, text: str, generation: int) -> bool:
        """Commit only the currently silent generation（当前静默代次）."""
        if generation != self._generation:
            return False
        self._replace_document(self._bounded(text))
        return True

    async def close(self) -> None:
        self.user_started()
