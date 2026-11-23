"""会话内悬浮图文路由：仅向申请并持有当前本机窗口代次的连接投递。"""
from dataclasses import dataclass

from ..agent.display_decision import DisplayDecision
from ..core.events import event


@dataclass(frozen=True)
class DisplayTarget:
    connection_id: str  # connection_id（本机实时连接标识）。
    generation: int  # generation（正式悬浮模式代次）。


class FloatingDisplaySession:
    def __init__(self, runtime):
        self.runtime = runtime
        self._target: DisplayTarget | None = None
        self._delivery = 0  # _delivery（核心投递递增序号）。
        self._status = ""  # _status（最新一条投递的状态）。
        self.feedback = ""  # feedback（供下一次模型决策参考的短回执）。

    def target(self) -> DisplayTarget | None:
        lease = self.runtime.device_registry.active
        return self._target if lease is None or lease.device_id == "local" else None

    def set_mode(self, connection_id: str, payload: dict) -> bool:
        if set(payload) != {"enabled", "generation"}:
            return False
        enabled, generation = payload["enabled"], payload["generation"]
        if type(enabled) is not bool or type(generation) is not int or not 0 <= generation < 2**53:
            return False
        current = self._target
        if enabled:
            if current and current.connection_id == connection_id and current.generation >= generation:
                return current.generation == generation
            self._target = DisplayTarget(connection_id, generation)
        elif current and current.connection_id == connection_id and current.generation <= generation:
            self._target = None
        else:
            return True
        self.feedback = self._status = ""
        return True

    def disconnect(self, connection_id: str) -> None:
        if self._target and self._target.connection_id == connection_id:
            self._target = None
            self.feedback = self._status = ""

    async def submit(self, target: DisplayTarget, conversation_id: str, turn_id: str | None,
                     decision: DisplayDecision) -> int | None:
        # 校验派发时冻结的连接、模式和会话，避免晚到的模型响应污染新窗口。
        if target != self.target() or conversation_id != self.runtime.conversation.conversation_id:
            return
        if decision.error:
            self.feedback = decision.error
            return
        return await self._publish(target, turn_id, decision)

    async def clear(self) -> None:
        if self._target is not None:
            await self._publish(self._target, None, DisplayDecision())

    async def _publish(self, target: DisplayTarget, turn_id: str | None, decision: DisplayDecision) -> int:
        self._delivery += 1
        delivery = self._delivery  # delivery（本次投递编号）：等待期间其他来源可以提交后续内容。
        self._status = "pending"
        self.feedback = ""
        await self.runtime.bus.publish(event(
            "display.command", turn_id, _display_connection=target.connection_id,
            generation=target.generation, delivery=delivery,
            content=decision.content, duration_seconds=decision.duration_seconds,
        ))
        return delivery

    def receive(self, connection_id: str, payload: dict) -> bool:
        if (self._target is None or self._target.connection_id != connection_id
                or set(payload) != {"generation", "delivery", "status", "reason"}):
            return False
        generation, delivery, status, reason = (payload[key] for key in ("generation", "delivery", "status", "reason"))
        if (type(generation) is not int or generation != self._target.generation
                or type(delivery) is not int or delivery != self._delivery
                or not isinstance(status, str) or not (reason is None or isinstance(reason, str))):
            return False
        if self._status == "pending" and status in {"visible", "rejected", "cleared"}:
            self._status = status
        elif self._status == "visible" and status == "expired":
            self._status = status
        else:
            return False
        self.feedback = {
            "visible": "图文已经显示。", "cleared": "图文已经收起。", "expired": "图文展示已到期。",
            "rejected": "上一条图文无法加载或排版，仍可在需要时缩短内容或更换为可用的图片地址。",
        }[status]
        return True
