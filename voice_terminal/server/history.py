from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.events import utc_now_iso


@dataclass
class TurnRecord:
    turn_id: str
    transcript: str
    response: str
    completed_at: str
    latency: dict[str, int | float | None]
    delivery: dict[str, Any] = field(default_factory=dict)
    episode_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class InMemoryHistory:
    def __init__(self, limit: int = 10) -> None:
        self.limit = limit
        self.records: list[TurnRecord] = []
        self._system_message = {
            "role": "system",
            "content": "你是本地实时语音终端的中文助手。回答简洁、直接，优先给出可执行的信息；工具调用通过工具接口进行，不要在面向用户的正文中输出或朗读工具参数。",
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

    def append_exchange(
        self,
        user_content: str,
        assistant_content: str,
        *,
        delivery: dict[str, Any] | None = None,
    ) -> None:
        """Commit one completed user/assistant exchange together."""

        if user_content.strip():
            self.messages.append({"role": "user", "content": user_content})
        if assistant_content.strip():
            self.messages.append({"role": "assistant", "content": assistant_content})
        if delivery and delivery.get("termination") == "interrupted":
            self.messages.append({"role": "system", "content": self._interruption_notice(delivery)})

    @staticmethod
    def _interruption_notice(delivery: dict[str, Any]) -> str:
        heard = str(delivery.get("heard_text") or "").strip()
        uncertain = str(delivery.get("uncertain_text") or "").strip()
        unheard = str(delivery.get("unheard_text") or "").strip()
        precision = str(delivery.get("precision") or "unknown")
        parts = [
            "上一轮助手回复被用户的真人语音打断。",
            f"已确认传达的内容：{heard or '无'}。",
        ]
        if uncertain:
            parts.append(f"用户插话重叠或边界不确定的内容：{uncertain}。")
        elif precision == "unknown":
            parts.append("插话时的精确文本边界未知，不要把未确认部分当作已听到。")
        if unheard:
            parts.append(f"确认未传达的内容：{unheard}。")
        parts.append("请根据用户的新输入回答，不要假定用户已经知道未播放内容。")
        return "".join(parts)

    def append_record(
        self,
        *,
        turn_id: str,
        transcript: str,
        response: str,
        latency: dict[str, int | float | None],
        delivery: dict[str, Any] | None = None,
        episode_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=turn_id,
            transcript=transcript,
            response=response,
            completed_at=utc_now_iso(),
            episode_id=episode_id,
            details=dict(details or {}),
            latency=dict(latency),
            delivery=dict(delivery or {}),
        )
        self.records = [item for item in self.records if item.turn_id != turn_id]
        self.records.insert(0, record)
        del self.records[self.limit :]
        return record
