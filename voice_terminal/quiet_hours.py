"""每日被动静默（quiet hours）；用户唤醒窗口仍可正常交互。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re


class PassiveQuietSuppressed(Exception):
    """passive quiet（被动静默）阻止合成或播放。"""


@dataclass(frozen=True)
class QuietHoursConfig:
    enabled: bool = False  # enabled（启用被动静默时间）。
    start: str = "23:00"  # start（每日本地开始时间，包含边界）。
    end: str = "07:00"  # end（每日本地结束时间，不包含边界）。

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled（启用开关）必须是布尔值。")
        for name, value in (("start", self.start), ("end", self.end)):
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError(f"{name}（本地时间）必须为 HH:mm（小时:分钟）。")
        if self.start == self.end:
            raise ValueError("被动静默开始和结束时间不能相同。")

    def snapshot(self) -> dict:
        return asdict(self)


class QuietHoursPolicy:
    def __init__(self, config: QuietHoursConfig | None = None) -> None:
        self.config = config or QuietHoursConfig()
        self.user_interaction = False

    def within_hours(self, now: datetime | None = None) -> bool:
        if not self.config.enabled:
            return False
        current = (now or datetime.now().astimezone()).strftime("%H:%M")
        start, end = self.config.start, self.config.end
        return start <= current < end if start < end else current >= start or current < end

    def passive(self, now: datetime | None = None) -> bool:
        return self.within_hours(now) and not self.user_interaction

    def snapshot(self) -> dict:
        return {"passive_quiet": self.passive(), "user_interaction": self.user_interaction,
                "within_quiet_hours": self.within_hours()}
