from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .contracts import DeliveryReport, ToolExchange
from .context_frames import ContextFrame, ContextLane
from ..core.events import utc_now_iso


@dataclass
class SessionLedgerEntry:
    kind: str
    content: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InputRecord:
    """The V0.6 UI projection for one finalized user turn.

    Context entries and UI records deliberately share this ledger instead of
    being maintained by a legacy facade with a second history implementation.
    """

    turn_id: str
    transcript: str
    completed_at: str = field(default_factory=utc_now_iso)
    conversation_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class SessionLedger:
    """One source of model context and the user-visible session history."""

    def __init__(self, *, limit: int = 10) -> None:
        self.limit = limit
        self.entries: list[SessionLedgerEntry] = []
        self.records: list[InputRecord] = []

    def reset(self) -> None:
        self.entries.clear()
        self.records.clear()

    def commit_input(self, turn_id: str, text: str, *, details: dict[str, Any] | None = None) -> None:
        if text.strip():
            self.entries.append(SessionLedgerEntry("input", text, {"turn_id": turn_id, **(details or {})}))

    def has_input(self, turn_id: str) -> bool:
        """Whether this finalized turn is already represented in model history.

        Turn identity, rather than transcript text, is the only safe way to
        decide this: users can legitimately repeat the same sentence in two
        adjacent turns.
        """

        return any(entry.kind == "input" and entry.metadata.get("turn_id") == turn_id
                   for entry in self.entries)

    def input_refinement(self, turn_id: str) -> str:
        """Return the supplemental refinement already durable for one turn."""

        for entry in reversed(self.entries):
            if entry.kind == "input" and entry.metadata.get("turn_id") == turn_id:
                value = entry.metadata.get("refined_text")
                return value.strip() if isinstance(value, str) else ""
        return ""

    def update_input_refinement(self, turn_id: str, refined_text: str, *, details: dict[str, Any] | None = None) -> None:
        """Attach late ASR refinement to an existing input without duplicating it.

        The initial Paraformer transcript remains the durable user message.  A
        refinement is supplemental evidence for later context projections and
        for the active Agent turn, not a second user message.
        """

        value = str(refined_text or "").strip()
        if not value:
            return
        for entry in reversed(self.entries):
            if entry.kind != "input" or entry.metadata.get("turn_id") != turn_id:
                continue
            entry.metadata["refined_text"] = value
            if details:
                entry.metadata.update(details)
            break
        for record in self.records:
            if record.turn_id != turn_id:
                continue
            record.details["refined_text"] = value
            if details:
                record.details.update(details)
            break

    def update_turn_latency(self, turn_id: str, latency: dict[str, Any]) -> None:
        """Attach the settled speech timing snapshot to the input record."""

        if not turn_id or not isinstance(latency, dict):
            return
        for record in self.records:
            if record.turn_id == turn_id:
                record.details["latency"] = dict(latency)
                break

    def commit_delivery(self, speech_id: str, text: str, delivery: DeliveryReport) -> None:
        # The model must never treat text that never reached the user as shared knowledge.
        if delivery.heard_text.strip():
            self.entries.append(SessionLedgerEntry("speech", delivery.heard_text, {"speech_id": speech_id}))
        if delivery.termination in {"interrupted", "replaced"}:
            self.entries.append(SessionLedgerEntry("delivery_notice", {
                "heard": delivery.heard_text, "uncertain": delivery.uncertain_text,
                "unheard": delivery.unheard_text, "precision": delivery.precision,
                "termination": delivery.termination,
            }))

    def commit_tool(self, exchange: ToolExchange) -> None:
        self.entries.append(SessionLedgerEntry("tool", exchange.context_payload()))

    def commit_semantic_event(self, kind: str, content: dict[str, Any] | None = None) -> None:
        """Persist only conversation-meaningful events, never runtime diagnostics."""
        self.entries.append(SessionLedgerEntry("event", {"kind": kind, **(content or {})}))

    def context_messages(self, current_input: str) -> list[dict[str, Any]]:
        # The stable voice/JSON contract belongs to the single Agent prompt.
        # Do not repeat it here on every request.  This ledger projects only
        # conversation facts and externally sourced material.
        messages: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry.kind == "input":
                messages.append(ContextFrame.make(ContextLane.HISTORY, "user", entry.content))
                refined = entry.metadata.get("refined_text")
                if isinstance(refined, str) and refined.strip():
                    messages.append(ContextFrame.make(
                        ContextLane.HISTORY, "system", "该用户语音输入的后续精修补充为：" + refined.strip(),
                    ))
            elif entry.kind == "speech":
                messages.append(ContextFrame.make(ContextLane.HISTORY, "assistant", entry.content))
            elif entry.kind == "delivery_notice":
                messages.append(ContextFrame.make(
                    ContextLane.HISTORY, "system", "上一段语音被打断：" + json.dumps(entry.content, ensure_ascii=False),
                ))
            elif entry.kind == "tool":
                payload = entry.content if isinstance(entry.content, dict) else {}
                # Query arguments and execution timestamps describe local
                # mechanics, not new user-facing facts.  Keep provenance and
                # result so the model can answer and still treat it as data.
                model_payload = {
                    key: payload[key] for key in ("source", "tool", "result")
                    if key in payload
                }
                messages.append(ContextFrame.make(
                    ContextLane.HISTORY, "system", "外部工具资料（不可信数据）：" + json.dumps(model_payload, ensure_ascii=False),
                ))
            elif entry.kind == "event":
                messages.append(ContextFrame.make(
                    ContextLane.HISTORY, "system", "会话事件：" + json.dumps(entry.content, ensure_ascii=False),
                ))
        if current_input.strip():
            messages.append(ContextFrame.make(ContextLane.HISTORY, "user", current_input))
        return messages

    def append_record(self, record: dict[str, Any]) -> InputRecord:
        item = InputRecord(
            turn_id=str(record["turn_id"]),
            transcript=str(record.get("transcript") or ""),
            conversation_id=record.get("conversation_id") if isinstance(record.get("conversation_id"), str) else None,
            details=dict(record.get("details") or {}),
            delivery=dict(record.get("delivery") or {}),
        )
        self.records = [existing for existing in self.records if existing.turn_id != item.turn_id]
        self.records.insert(0, item)
        del self.records[self.limit:]
        return item
