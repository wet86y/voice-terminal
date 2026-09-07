from __future__ import annotations

from typing import Any

from ..server.history import TurnRecord
from ..session.ledger import SessionLedger


class SessionLedgerFacade:
    """Small compatibility facade while audio mechanics migrate to InputTurn names.

    It deliberately has one backing ledger; `messages` and UI records are
    projections, not separate stores.
    """
    def __init__(self, limit: int = 10) -> None:
        self.ledger = SessionLedger(limit=limit)
        self.messages: list[dict[str, Any]] = self.ledger.context_messages("")
        self.records: list[TurnRecord] = []

    def _refresh_messages(self) -> None:
        self.messages = self.ledger.context_messages("")

    def reset(self) -> None:
        self.ledger.reset(); self.records.clear(); self._refresh_messages()

    def append_exchange(self, user_content: str, assistant_content: str, *, delivery: dict[str, Any] | None = None) -> None:
        self.ledger.commit_input("legacy-input", user_content)
        from ..session.contracts import DeliveryReport
        self.ledger.commit_delivery("legacy-speech", assistant_content, DeliveryReport.from_payload(delivery, termination="completed"))
        self._refresh_messages()

    def commit_input(self, input_id: str, text: str, *, details: dict[str, Any] | None = None) -> None:
        self.ledger.commit_input(input_id, text, details=details)
        self._refresh_messages()

    def commit_delivery(self, speech_id: str, text: str, delivery: dict[str, Any] | None, *, termination: str) -> None:
        from ..session.contracts import DeliveryReport
        self.ledger.commit_delivery(speech_id, text, DeliveryReport.from_payload(delivery, termination=termination))
        self._refresh_messages()

    def append_record(self, **kwargs: Any) -> TurnRecord:
        record = TurnRecord(**kwargs)
        self.records = [item for item in self.records if item.turn_id != record.turn_id]
        self.records.insert(0, record)
        del self.records[self.ledger.limit:]
        self.ledger.append_record(record.to_payload())
        return record
