"""Retractable text frontier and submitted-clause accounting."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from ..tts.scheduling import TtsSegmentReceipt

ADMISSION_STAGING_CHARS = 6000

@dataclass
class SubmittedClause:
    """One sentence-window block already accepted by the TTS provider.

    Text becomes irreversible as soon as ``send_text`` succeeds.  The PCM
    boundary arrives later, so it is deliberately optional until the
    provider's per-block receipt is observed.
    """

    index: int
    text: str
    end_received_bytes: int | None = None


class AdmissionState:
    """Retractable text frontier shared by one live speech transaction.

    ``sent_chars`` is the only irreversible boundary of an accepted response.
    Everything after it is still held locally -- in the handle queue, the merge
    buffer or this staging deque -- so a later LLM decision may replace that
    tail without interrupting audible playback or opening a new TTS session.

    Every mutating method is synchronous, so under the event loop they are
    atomic with respect to each other and no extra lock is required.
    """

    def __init__(self, *, lead_ms: int, char_limit: int = ADMISSION_STAGING_CHARS,
                 restage=None, sentence_window: bool = False) -> None:
        self.lead_ms = lead_ms
        self.char_limit = char_limit
        self.sentence_window = sentence_window
        self.staged: deque[str] = deque()
        self.submitted_clauses: deque[SubmittedClause] = deque()
        self.last_completed_boundary_bytes = 0
        self.staged_chars = 0
        self.sent_chars = 0
        self.epoch = 0
        self.closed = False
        self.producer_done = False  # producer_done（本地原始文本已全部分段入队）。
        self.sending = False  # sending（正在向服务端提交，确认完成前禁止修订）。
        self.overrun_reported = False
        self._restage = restage or (lambda value: [value] if value else [])
        self.signal = asyncio.Event()

    @property
    def rewritable(self) -> bool:
        """Whether an unsent tail still exists that a replacement can reach."""

        return not self.closed and not self.sending

    @property
    def overrun(self) -> bool:
        """Whether the staging budget was exceeded and the window must yield."""

        return self.staged_chars > self.char_limit

    def push(self, segment: str) -> None:
        self.staged.append(segment)
        self.staged_chars += len(segment)
        self.signal.set()

    def pop(self) -> str | None:
        if not self.staged:
            return None
        segment = self.staged.popleft()
        self.staged_chars -= len(segment)
        return segment

    def pop_sentence_block(self, *, min_effective_chars: int,
                           allow_short: bool = False) -> str | None:
        """pop_sentence_block（取锁定候选块）：累加完整段，达到有效字符下限。

        中文、字母和数字计数，标点、空白与符号不计。未凑齐的段留在
        staged（可改队列）中，尾巴改写仍能全部替换；只有生成结束时
        allow_short（允许不足下限的最后一块）才释放剩余段。
        """
        if min_effective_chars <= 0:
            raise ValueError("min_effective_chars（最少有效字符）必须为正整数")
        segments = []
        effective_chars = 0
        for segment in self.staged:
            segments.append(segment)
            effective_chars += sum(char.isalnum() for char in segment)
            if effective_chars >= min_effective_chars:
                break
        if not segments or (effective_chars < min_effective_chars and not allow_short):
            return None
        for _ in segments:
            self.pop()
        return "".join(segments)

    def close(self) -> None:
        self.closed = True
        self.signal.set()

    def commit_segment(self, text: str, *, receipt_index: int | None = None) -> None:
        """Atomically advance the irreversible frontier after provider acceptance."""

        self.sent_chars += len(text)
        if self.sentence_window:
            index = len(self.submitted_clauses) if receipt_index is None else int(receipt_index)
            self.submitted_clauses.append(SubmittedClause(index=index, text=text))

    def complete_segment(self, receipt: TtsSegmentReceipt) -> None:
        """Attach a cumulative PCM boundary to its submitted text block."""

        matching = next(
            (item for item in self.submitted_clauses if item.index == receipt.index),
            None,
        )
        if matching is None:
            raise RuntimeError("TTS 短句块回执没有对应的已提交文本")
        if matching.end_received_bytes is not None:
            raise RuntimeError("TTS 短句块回执重复")
        if receipt.text_chars != len(matching.text):
            raise RuntimeError("TTS 短句块回执文本长度不匹配")
        if receipt.end_received_bytes <= self.last_completed_boundary_bytes:
            raise RuntimeError("TTS 短句块回执边界没有递增")
        matching.end_received_bytes = receipt.end_received_bytes
        self.last_completed_boundary_bytes = receipt.end_received_bytes

    @staticmethod
    def _boundary_ms(item: SubmittedClause, bytes_per_second: int) -> int | None:
        if item.end_received_bytes is None or bytes_per_second <= 0:
            return None
        return int(item.end_received_bytes * 1000 / bytes_per_second)

    def active_clauses(self, *, output_estimated_ms: int, bytes_per_second: int) -> tuple[SubmittedClause, ...]:
        """Return current/prefetched blocks without mutating scheduling state."""

        active: list[SubmittedClause] = []
        for item in self.submitted_clauses:
            boundary_ms = self._boundary_ms(item, bytes_per_second)
            if boundary_ms is not None and output_estimated_ms >= boundary_ms:
                continue
            active.append(item)
        return tuple(active)

    def prune_played(self, *, output_estimated_ms: int, bytes_per_second: int) -> list[tuple[int, bool]]:
        """Drop played blocks and report their boundary/successor state."""

        removed: list[tuple[int, bool]] = []
        while self.submitted_clauses:
            boundary_ms = self._boundary_ms(self.submitted_clauses[0], bytes_per_second)
            if boundary_ms is None or output_estimated_ms < boundary_ms:
                break
            removed.append((boundary_ms, len(self.submitted_clauses) > 1))
            self.submitted_clauses.popleft()
        return removed

    def model_text_parts(self, text: str, *, output_estimated_ms: int = 0,
                         bytes_per_second: int = 0) -> tuple[dict[str, str], bool]:
        """Project the accepted text once into four non-overlapping parts.

        The returned boolean is false when active clause bookkeeping does not
        exactly describe the submitted prefix.  Callers must then expose the
        conservative all-committed fallback rather than promise a rewrite.
        """

        if not isinstance(text, str) or not isinstance(self.sent_chars, int):
            return {
                "committed_prefix": str(text or ""),
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }, False
        if self.sent_chars < 0 or self.sent_chars > len(text):
            return {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }, False
        submitted = text[:self.sent_chars]
        revisable = text[self.sent_chars:]
        active = self.active_clauses(
            output_estimated_ms=output_estimated_ms,
            bytes_per_second=bytes_per_second,
        ) if self.sentence_window else ()
        if len(active) > 2:
            return {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }, False
        current = active[0].text if active else ""
        prefetched = active[1].text if len(active) > 1 else ""
        active_suffix = current + prefetched
        if active_suffix and not submitted.endswith(active_suffix):
            return {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }, False
        committed = submitted[:-len(active_suffix)] if active_suffix else submitted
        parts = {
            "committed_prefix": committed,
            "current_clause": current,
            "prefetched_clause": prefetched,
            "revisable_tail": revisable,
        }
        valid = "".join(parts.values()) == text
        if not valid:
            return {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }, False
        return parts, True

    def replace_unsent(self, suffix: str) -> int:
        """Drop every unsent frame and stage ``suffix`` in its place.

        Returns the number of dropped characters, or ``-1`` when this
        transaction can no longer accept a replacement.
        """

        if not self.rewritable:
            return -1
        dropped = self.staged_chars
        self.staged.clear()
        self.staged_chars = 0
        # Bumping the epoch invalidates a frame the sender may already hold
        # between popping it and handing it to the provider.
        self.epoch += 1
        replacements = self._restage(suffix)
        for segment in replacements:
            if segment:
                self.push(segment)
        if not replacements:
            self.signal.set()
        return dropped
