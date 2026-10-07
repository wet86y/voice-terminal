"""Conversation identity and explicit cancellation ownership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from .contracts import new_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CancellationScope:
    scope_id: str
    turn_id: str | None


class ConversationRuntime:
    """Session identity plus explicit cancellation scope ownership."""

    def __init__(self) -> None:
        self.conversation_id = new_id("conversation")
        self.current_turn_id: str | None = None
        self._scope_id = new_id("scope")
        self._calendar_baseline = self._calendar_text()
        self._calendar_baseline_day = self._calendar_date()
        self._calendar_day = self._calendar_baseline_day

    @staticmethod
    def _now():
        return datetime.now().astimezone()

    @classmethod
    def _calendar_date(cls) -> str:
        return cls._now().strftime("%Y-%m-%d")

    @classmethod
    def _calendar_text(cls) -> str:
        now = cls._now()
        weekdays = "一二三四五六日"
        offset = now.strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "本地时区"
        return f"会话日期基准：{now:%Y-%m-%d}，星期{weekdays[now.weekday()]}，{offset}。"

    @property
    def calendar_baseline(self) -> str:
        return self._calendar_baseline

    def calendar_epoch_if_changed(self) -> str | None:
        """Return a tail-only date correction after local midnight."""

        day = self._calendar_date()
        # The epoch is a current-request fact, not a one-shot event.  Once a
        # session crosses midnight every later request must still be able to
        # interpret its stable, original date baseline correctly.
        if day != self._calendar_day:
            self._calendar_day = day
        if day == self._calendar_baseline_day:
            return None
        return self._calendar_text().replace("会话日期基准", "当前日期 epoch")

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def begin_turn(self, turn_id: str) -> CancellationScope:
        self.current_turn_id = turn_id
        self._scope_id = new_id("scope")
        return CancellationScope(self._scope_id, turn_id)

    def cancel(self, *, turn_id: str | None = None, reason: str = "cancelled") -> CancellationScope:
        if turn_id is None or turn_id == self.current_turn_id:
            self._scope_id = new_id("scope")
            logger.info("conversation.scope_cancelled scope_id=%s reason=%s conversation_id=%s turn_id=%s",
                        self._scope_id, reason, self.conversation_id, self.current_turn_id)
        return CancellationScope(self._scope_id, self.current_turn_id)

    def is_scope_current(self, scope_id: str, turn_id: str | None = None) -> bool:
        return (scope_id == self._scope_id
                and (turn_id is None or turn_id == self.current_turn_id))

    def reset(self) -> None:
        self._scope_id = new_id("scope")
        self.conversation_id = new_id("conversation")
        self.current_turn_id = None
        self._calendar_baseline = self._calendar_text()
        self._calendar_baseline_day = self._calendar_date()
        self._calendar_day = self._calendar_baseline_day

    def persistence_snapshot(self) -> dict[str, str]:
        """保留 conversation_id（会话标识）、calendar_baseline（日期基准）、calendar_baseline_day（基准日期）；取消作用域重新生成。"""
        return {"conversation_id": self.conversation_id, "calendar_baseline": self._calendar_baseline,
                "calendar_baseline_day": self._calendar_baseline_day}

    def restore(self, snapshot: dict[str, str]) -> None:
        conversation_id = snapshot["conversation_id"]
        if not isinstance(conversation_id, str) or not conversation_id.startswith(("conversation-", "conversation_")):
            raise ValueError("语音会话恢复标识无效")
        self.conversation_id = conversation_id
        self._calendar_baseline = snapshot["calendar_baseline"]
        self._calendar_baseline_day = snapshot["calendar_baseline_day"]
        self._calendar_day = self._calendar_date()
