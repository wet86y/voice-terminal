from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..core.events import utc_now_iso


@dataclass
class TurnRecord:
    turn_id: str
    transcript: str
    response: str
    completed_at: str
    latency: dict[str, int | float | None]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class InMemoryHistory:
    def __init__(self, limit: int = 10) -> None:
        self.limit = limit
        self.records: list[TurnRecord] = []
        self._system_message = {
            "role": "system",
            "content": "你是本地实时语音终端的中文助手。回答简洁、直接，优先给出可执行的信息；不要输出工具调用格式。",
        }
        self.messages: list[dict[str, str]] = [dict(self._system_message)]

    def reset(self) -> None:
        self.records.clear()
        self.messages[:] = [dict(self._system_message)]

    def append_context(self, role: str, content: str) -> None:
        if content.strip():
            self.messages.append({"role": role, "content": content})

    def messages_for_user(self, content: str) -> list[dict[str, str]]:
        """Build a request context without mutating committed history."""

        messages = [dict(message) for message in self.messages]
        if content.strip():
            messages.append({"role": "user", "content": content})
        return messages

    def append_exchange(self, user_content: str, assistant_content: str) -> None:
        """Commit one completed user/assistant exchange together."""

        if user_content.strip():
            self.messages.append({"role": "user", "content": user_content})
        if assistant_content.strip():
            self.messages.append({"role": "assistant", "content": assistant_content})

    def append_record(
        self,
        *,
        turn_id: str,
        transcript: str,
        response: str,
        latency: dict[str, int | float | None],
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=turn_id,
            transcript=transcript,
            response=response,
            completed_at=utc_now_iso(),
            latency=dict(latency),
        )
        self.records = [item for item in self.records if item.turn_id != turn_id]
        self.records.insert(0, record)
        del self.records[self.limit :]
        return record
