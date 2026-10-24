"""Continue listening（持续倾听）：消费驱动的本地注入余额，不创建计时任务。"""
from __future__ import annotations

from ..session.secondary_mailbox import SecondaryMailbox
import logging

logger = logging.getLogger(__name__)


LISTENING_ACTION = {
    "name": "continue_listening",
    "description": "当你希望在当前对话中继续留意用户的后续反馈时，可以请求后续评估机会。后续评估时，可以继续等待，也可以结合话题自然回应或询问。count（注入次数）为本次新增的1、2或3次二等信箱注入。标记可能随普通请求提前消费，不对应固定时长或发声次数。调用本工具与本次是否开口分别决定。",
    "parameters": {"type": "object", "properties": {
        "count": {"type": "integer", "enum": [1, 2, 3], "description": "本次新增注入次数，整数1、2或3"},
    }, "required": ["count"], "additionalProperties": False},
}


class ListeningRequests:
    """remaining（待注入余额）最多4，独立保留至多一条已注入标记。"""

    def __init__(self, mailbox: SecondaryMailbox):
        self.mailbox = mailbox
        self.remaining = 0
        self.scope = mailbox.new_scope()
        self.enabled = True
        self._applied: set[str] = set()
        self.injected = 0  # injected（累计实际注入数）。
        self.truncated = 0  # truncated（累计容量截断数）。
        self.cleared = 0  # cleared（生命周期清理次数）。

    def snapshot(self) -> dict:
        return {"remaining": self.remaining, "pending": self.mailbox.has_scope(self.scope)}

    def request(self, count: int, *, scope: str, logical_id: str) -> dict:
        if type(count) is not int or count not in (1, 2, 3):
            raise ValueError("count（注入次数）必须为整数1、2或3")
        if not self.enabled or scope != self.scope or not self.mailbox.scope_valid(scope):
            return {"accepted": 0, "reason": "expired_input_scope"}
        if logical_id in self._applied:
            return {"accepted": 0, "reason": "already_applied"}
        self._applied.add(logical_id)
        accepted = min(count, 4 - self.remaining)
        self.remaining += accepted
        self.truncated += count - accepted
        logger.info("listening.request（倾听追加） requested_count（请求次数）=%s accepted（受理次数）=%s truncated（截断次数）=%s",
                    count, accepted, count - accepted)
        injected = self.refill(first=True)
        return {"accepted": accepted, "truncated": count - accepted, "injected": injected,
                **self.snapshot()}

    def refill(self, *, first: bool = False) -> bool:
        if (not self.enabled or not self.remaining or not self.mailbox.scope_valid(self.scope)
                or self.snapshot()["pending"] or (not first and self.mailbox)):
            return False
        self.mailbox.publish(source="main_llm", kind="listening_check", payload={},
                             scope=self.scope, coalesce_key="active")
        self.remaining -= 1
        self.injected += 1
        logger.info("listening.injected（倾听标记注入） remaining（剩余次数）=%s", self.remaining)
        return True

    def suspend(self) -> None:
        self.enabled = False

    def close(self) -> None:
        state = self.snapshot()
        self.mailbox.invalidate(self.scope)
        self.remaining = 0
        self.enabled = False
        self._applied.clear()
        if state["remaining"] or state["pending"]:
            self.cleared += 1
            logger.info("listening.cleared（倾听计划清理） remaining（清理前余额）=%s pending（是否已注入）=%s",
                        state["remaining"], state["pending"])

    def open(self) -> None:
        if self.enabled and self.mailbox.scope_valid(self.scope):
            return
        self.close()
        self.scope = self.mailbox.new_scope()
        self.enabled = True
