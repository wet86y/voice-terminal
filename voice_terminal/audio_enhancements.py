"""Audio enhancements（音频增强）：持久化选项与请求级声学信箱。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Mapping


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

    def __init__(self):
        self._rows: list[dict] = []
        self._active: int | None = None
        self._continuous: dict | None = None  # 已消费后仍保留当前持续观察的开始时间。
        self._sequence = 0
        self._scope = 0
        self._last_end = -1

    def clear(self):
        self._rows.clear()
        self._active = None
        self._continuous = None
        self._last_end = -1
        self._scope += 1

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
            if not any(row["id"] == active["id"] for row in self._rows):
                self._rows.append(active)
            return
        self._sequence += 1
        self._active = self._sequence
        self._rows.append({"id": self._sequence, "scope": self._scope, "labels": labels,
                           "start_sample": start_sample, "end_sample": end_sample})
        self._continuous = self._rows[-1]

    def snapshot(self) -> list[dict]:
        return deepcopy(self._rows)

    def valid(self, rows: list[dict]) -> list[dict]:
        return deepcopy([row for row in rows if row.get("scope") == self._scope])

    def consume(self, captured: list[dict]):
        for old in self.valid(captured):
            for current in tuple(self._rows):
                if current["id"] != old["id"]:
                    continue
                if current["end_sample"] <= old["end_sample"]:
                    self._rows.remove(current)
                # 持续观察已延长时保留新版本及原开始时间；旧快照只确认旧的最近观测。

    @staticmethod
    def project(rows: list[dict]) -> list[dict]:
        return [{"labels": list(row["labels"]),  # labels（声音类别列表）。
                 "start_ms": round(row["start_sample"] / 16),  # start_ms（采集时间轴起点）。
                 "end_ms": round(row["end_sample"] / 16)}  # end_ms（最近观测时间）。
                for row in rows]
