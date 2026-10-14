"""Ordered, monotonic projection of one session's conversation.

The product rail is a chat surface: entries must keep the order they were
created in, a system-owned line (the KWS wake acknowledgement) must land where
it really happened instead of after the whole history, and a page reload must
be able to restore the same view.  The session ledger already owns the durable
conversation — the finalized transcript plus the text the user actually heard —
so this module gives that history one explicit, server-owned order.

Ordering is a single ``seq`` assigned at append time.  Nothing in the client has
to infer chronology from three unrelated projections again, and no entry ever
moves once it has a position.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core.events import utc_now_iso

USER_TURN = "user_turn"
ASSISTANT_SPEECH = "assistant_speech"
SYSTEM_SPEECH = "system_speech"

# One page of history is what a reconnecting client needs to render the rail.
# The bound keeps the ``subscribe`` snapshot small; the model context is owned
# by :class:`~voice_terminal.session.ledger.SessionLedger.entries`, not here.
DEFAULT_LIMIT = 200

EntrySink = Callable[[dict[str, Any]], None]


def speech_kind(owner_scope: Any) -> str:
    """Map a speech transaction's owner scope onto its conversation kind.

    System checks (the wake acknowledgement, the final-silence check) are
    conversation lines in their own right: the user hears them, so they are
    recorded with their own place in the order rather than attached to the turn
    that happens to arrive next.
    """

    return SYSTEM_SPEECH if owner_scope == "system_check" else ASSISTANT_SPEECH


class ConversationLog:
    """Append-only, bounded conversation view with a monotonic ``seq``."""

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self.entries: list[dict[str, Any]] = []
        self._index: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._sink: EntrySink | None = None
        self.on_change: Callable[[], None] | None = None  # on_change（持久化检查点通知）。

    @property
    def latest_seq(self) -> int:
        return self._seq

    def set_sink(self, sink: EntrySink | None) -> None:
        """Register the publisher of :func:`append`.

        The sink is deliberately synchronous: every commit path in the runtime
        is sync-capable, and scheduling the publication is the runtime's job.
        """

        self._sink = sink

    @staticmethod
    def key_of(kind: str, conversation_id: str, turn_id: str, speech_id: str) -> str:
        """Stable identity of one conversation line.

        A user turn is one line per ``turn_id``; every speech transaction is one
        line per ``speech_id`` (a replacement is a new transaction, so it gets
        its own line instead of overwriting the interrupted one).
        """

        if kind == USER_TURN:
            return f"turn\u0000{conversation_id}\u0000{turn_id}"
        return f"speech\u0000{conversation_id}\u0000{speech_id}"

    def append(
        self,
        kind: str,
        *,
        conversation_id: str = "",
        turn_id: str = "",
        speech_id: str = "",
        text: str = "",
        status: str = "",
        delivery: dict[str, Any] | None = None,
        termination: str = "",
        at: str | None = None,
        attachments: list[dict] | None = None,  # attachments（图像描述，不携带编码）。
    ) -> dict[str, Any]:
        """Record one line and publish it.

        Re-appending the same line (a later settlement of a turn that was
        already committed) refreshes its content while keeping the original
        ``seq``, so a refresh can never reorder the rail.
        """

        key = self.key_of(kind, conversation_id, turn_id, speech_id)
        entry = self._index.get(key)
        if entry is None:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "kind": kind,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "speech_id": speech_id,
                "text": str(text or ""),
                "status": str(status or ""),
                "delivery": dict(delivery or {}),
                "termination": str(termination or ""),
                "at": at or utc_now_iso(),
            }
            self.entries.append(entry)
            self._index[key] = entry
            self._trim()
        else:
            entry["text"] = str(text or "") or entry.get("text", "")
            entry["status"] = str(status or "") or entry.get("status", "")
            entry["termination"] = str(termination or "") or entry.get("termination", "")
            if delivery:
                entry["delivery"] = dict(delivery)
            if conversation_id:
                entry["conversation_id"] = conversation_id
        if attachments is not None:
            entry["attachments"] = [dict(item) for item in attachments]
        published = dict(entry)
        published["delivery"] = dict(entry.get("delivery") or {})
        if self.on_change is not None:
            self.on_change()
        sink = self._sink
        if sink is not None:
            sink(published)
        return published

    def snapshot(self) -> dict[str, Any]:
        """Return the client-facing view: bounded entries plus the head ``seq``."""

        return {
            "entries": [
                {**entry, "delivery": dict(entry.get("delivery") or {})}
                for entry in self.entries
            ],
            "latest_seq": self._seq,
        }

    def reset(self) -> None:
        """Drop the whole conversation at a session boundary.

        The order restarts with the new session; a client always adopts the
        authoritative snapshot that accompanies a reset instead of merging
        across the boundary.
        """

        self.entries.clear()
        self._index.clear()
        self._seq = 0
        if self.on_change is not None:
            self.on_change()

    def restore(self, snapshot: dict[str, Any]) -> None:
        """restore（恢复聊天顺序）：沿用服务端序号，不重新发布历史消息。"""
        entries = snapshot["entries"]
        latest_seq = snapshot["latest_seq"]
        if not isinstance(entries, list) or not isinstance(latest_seq, int) or latest_seq < 0:
            raise ValueError("聊天记录恢复检查点无效")
        previous = 0
        index = {}
        restored = []
        for value in entries:
            entry = {**value, "delivery": dict(value.get("delivery") or {})}
            seq = entry["seq"]
            key = self.key_of(entry["kind"], entry["conversation_id"], entry["turn_id"], entry["speech_id"])
            if not isinstance(seq, int) or not previous < seq <= latest_seq or key in index:
                raise ValueError("聊天记录恢复顺序无效")
            previous = seq
            restored.append(entry)
            index[key] = entry
        self.entries, self._index, self._seq = restored, index, latest_seq
        self._trim()

    def _trim(self) -> None:
        while len(self.entries) > self.limit:
            expired = self.entries.pop(0)
            key = self.key_of(
                str(expired.get("kind") or ""),
                str(expired.get("conversation_id") or ""),
                str(expired.get("turn_id") or ""),
                str(expired.get("speech_id") or ""),
            )
            self._index.pop(key, None)
