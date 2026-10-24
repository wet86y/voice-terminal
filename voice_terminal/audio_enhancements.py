"""Audio enhancements（音频增强）：持久化选项与请求级声学信箱。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .session.secondary_mailbox import SecondaryMailbox


@dataclass(frozen=True)
class AudioEnhancements:
    dual_pass: bool = True  # dual_pass（双路精修开关）。
    emotion: bool = True  # emotion（情绪背景开关）。
    acoustic_events: bool = True  # acoustic_events（声学信箱开关）。

    @classmethod
    def from_mapping(cls, value: Mapping | None, *, legacy: bool = True):
        if type(legacy) is not bool:
            raise ValueError("旧精修总开关必须为布尔值")
        value = {} if value is None else value
        if not isinstance(value, Mapping) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("audio_enhancements（音频增强）字段无效")
        if any(type(item) is not bool for item in value.values()):
            raise ValueError("audio_enhancements（音频增强）的开关必须为布尔值")
        return cls(**{key: value.get(key, legacy) for key in cls.__dataclass_fields__})

    def snapshot(self) -> dict:
        return asdict(self)


class AcousticMailbox:
    """AcousticMailbox（声学信箱）：仅确认请求捕获的观察前缀。"""

    def __init__(self, mailbox: SecondaryMailbox | None = None):
        # 设置页只读取选项；实例化声学适配器时才加载会话模块。
        from .session.secondary_mailbox import SecondaryMailbox
        self.mailbox = SecondaryMailbox() if mailbox is None else mailbox
        self._mailbox_scope = self.mailbox.new_scope()
        self._active: int | None = None
        self._continuous: dict | None = None  # 已消费后仍保留当前持续观察的开始时间。
        self._sequence = 0
        self._scope = 0
        self._last_end = -1

    def clear(self):
        self.mailbox.invalidate(self._mailbox_scope)
        self._mailbox_scope = self.mailbox.new_scope()
        self._active = None
        self._continuous = None
        self._last_end = -1
        self._scope += 1

    def bind_mailbox(self, mailbox: SecondaryMailbox):
        """接入账本唯一存储；独立实例继续可用于音频聚合测试。"""
        if mailbox is self.mailbox:
            return
        rows = self.snapshot()
        self.mailbox.invalidate(self._mailbox_scope)
        self.mailbox = mailbox
        self._mailbox_scope = mailbox.new_scope()
        for row in rows:
            self._publish(row)

    def _publish(self, row: dict):
        if not self.mailbox.scope_valid(self._mailbox_scope):
            self._mailbox_scope = self.mailbox.new_scope()
        self.mailbox.publish(source="acoustic", kind="acoustic_observation",
            payload=self.project([row])[0], scope=self._mailbox_scope,
            coalesce_key=str(row["id"]),
            metadata={key: row[key] for key in ("id", "scope", "start_sample", "end_sample")})

    def interrupt(self):
        """暂停观察形成断点，保留未消费记录。"""
        self._active = None
        self._continuous = None

    def append(self, labels: list[str], start_sample: int, end_sample: int, label_ids: list[int] | None = None):
        if end_sample <= start_sample or end_sample <= self._last_end:
            return
        self._last_end = end_sample
        order = dict(zip(labels, label_ids)) if label_ids is not None and len(label_ids) == len(labels) else {}
        labels = sorted(set(label for label in labels if isinstance(label, str) and label),
                        key=lambda label: (order.get(label, 1000), label))
        if not labels:
            self.interrupt()
            return
        active = self._continuous
        if active is not None and active["labels"] == labels:
            active["end_sample"] = end_sample
            self._publish(active)
            return
        self._sequence += 1
        self._active = self._sequence
        self._continuous = {"id": self._sequence, "scope": self._scope, "labels": labels,
                            "start_sample": start_sample, "end_sample": end_sample}
        self._publish(self._continuous)

    def snapshot(self) -> list[dict]:
        return [{**row["metadata"], "labels": list(row["payload"]["labels"]),
                 "_mailbox_id": row["id"], "_mailbox_version": row["version"],
                 "_mailbox_scope": row["scope"]}
                for row in self.mailbox.snapshot().values() if row["scope"] == self._mailbox_scope]

    def valid(self, rows: list[dict]) -> list[dict]:
        return deepcopy([row for row in rows if row.get("scope") == self._scope
                         and self.mailbox.scope_valid(row.get("_mailbox_scope"))])

    def consume(self, captured: list[dict]):
        self.mailbox.consume({row["_mailbox_id"]: {
            "id": row["_mailbox_id"], "version": row["_mailbox_version"],
            "scope": row["_mailbox_scope"]} for row in self.valid(captured)})

    @staticmethod
    def project(rows: list[dict]) -> list[dict]:
        return [{"labels": list(row["labels"]),  # labels（声音类别列表）。
                 "start_ms": round(row["start_sample"] / 16),  # start_ms（采集时间轴起点）。
                 "end_ms": round(row["end_sample"] / 16)}  # end_ms（最近观测时间）。
                for row in rows]
