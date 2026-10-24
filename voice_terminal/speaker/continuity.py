"""跨轮主人资格：只由最终成立的输入边界交接，不由观察心跳续期。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OwnerContinuity:
    epoch: int = 0  # 资格代次；设置、明确反证或生命周期撤销时递增。
    source_turn_id: str = ""  # 最近提供连续结论的已结束轮次。
    normal_turn_id: str = ""  # 最近提供正常阈值主人证据的轮次。
    normal_end_at: float | None = None  # 该正常主人轮次的实际语音终点。
    continuing: bool = False  # 已经承接过放宽结论；不移动正常证据时钟。
    reason: str = "no_previous_owner"  # 当前无资格或撤销原因。

    def capture(self, at: float, config) -> dict:
        """at（实际开口时刻）；快照不因首段积累或推理耗时再次到期。"""
        if self.normal_end_at is None or at < self.normal_end_at:
            return {}
        limit = config.continuity_max_ms if self.continuing else config.continuity_window_ms
        deadline = self.normal_end_at + limit / 1000
        if limit <= 0 or at > deadline:
            return {}
        return {"source_turn_id": self.source_turn_id, "normal_turn_id": self.normal_turn_id,
                "normal_end_at": self.normal_end_at, "continuity_deadline": deadline,
                "continuity_epoch": self.epoch, "captured_at": at}

    def revoke(self, reason: str) -> None:
        self.epoch += 1
        self.source_turn_id = self.normal_turn_id = ""
        self.normal_end_at = None
        self.continuing, self.reason = False, reason

    def complete(self, turn, *, boundary_at: float) -> str:
        """只有最终语音边界调用；未知和仅放宽结果不产生新正常证据。"""
        if not turn.admitted or not turn.owner_confirmed:
            return "unchanged"
        if turn.normal_owner_epoch == self.epoch:
            self.source_turn_id = self.normal_turn_id = turn.turn_id
            self.normal_end_at, self.continuing = boundary_at, False
            self.reason = "normal_owner_boundary"
            return "renewed"
        grant = turn.continuity_grant
        if (turn.owner_confirmed and grant and grant["continuity_epoch"] == self.epoch
                and self.normal_end_at is not None):
            self.source_turn_id, self.continuing = turn.turn_id, True
            self.reason = "continued_without_renewal"
            return "continued"
        return "unchanged"

    def snapshot(self, config) -> dict:
        return {"source_turn_id": self.source_turn_id, "normal_turn_id": self.normal_turn_id,
                "normal_end_at": self.normal_end_at, "continuity_epoch": self.epoch,
                "continuing": self.continuing, "reason": self.reason,
                "entry_deadline": None if self.normal_end_at is None else self.normal_end_at + config.continuity_window_ms/1000,
                "max_deadline": None if self.normal_end_at is None else self.normal_end_at + config.continuity_max_ms/1000}
