"""Remote playback transactions（远端播放事务）：通过固定设备路由发送并核对回执。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..audio.player import PlaybackSnapshot
from .contracts import PlaybackOutcomeUnknown, PlaybackReplacementResult, PlaybackReplacementTicket
from .router import DeviceOutcomeUnknown, DeviceRouter

_SAFE_INTEGER = (1 << 53) - 1  # _SAFE_INTEGER（跨语言安全整数上限）。
_PRECISIONS = {"callback", "device_time", "estimated", "unknown"}  # _PRECISIONS（协议允许的精度编号）。


class RemotePlaybackTransactions:
    """RemotePlaybackTransactions（远端播放事务）：仅实现等待式控制面，不承载音频流。

    设备选择在构造时固定。每次调用生成唯一 request_id（请求编号）；未知结果会把该编号
    放入 PlaybackOutcomeUnknown（播放结果未知）异常，调用方只能查询同一任务，不能自动重发。
    """

    def __init__(self, router: DeviceRouter, device_id: str, *, timeout_seconds: float = 5.0):
        if not isinstance(device_id, str) or not device_id or device_id != device_id.strip() or len(device_id) > 128:
            raise ValueError("播放目标设备编号无效。")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 600):
            raise ValueError("远端播放操作时限必须大于零且不超过 600 秒。")
        self.router = router
        self.device_id = device_id  # device_id（固定执行设备编号）。
        self.timeout_seconds = float(timeout_seconds)  # timeout_seconds（单次回执等待上限，秒）。

    def _unknown(self, request_id: str, operation: str, message: str) -> PlaybackOutcomeUnknown:
        return PlaybackOutcomeUnknown(message, request_id=request_id,
                                      device_id=self.device_id, operation=operation)

    async def query_request(self, request_id: str):
        """query_request（只读原任务查询）：读取固定设备台账，不向设备重发操作。"""
        return await self.router.query(request_id=request_id, device_id=self.device_id)

    async def _execute(self, operation: str, arguments: dict[str, Any], *, timeout_seconds: float | None = None):
        request_id = uuid4().hex  # request_id（仅本次请求使用，未知时禁止自动生成重试）。
        try:
            record = await self.router.execute(
                request_id=request_id,
                device_id=self.device_id,
                operation=f"playback.{operation}",
                arguments=arguments,
                timeout_seconds=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            )
        except DeviceOutcomeUnknown as exc:
            raise self._unknown(request_id, operation, "设备未确认播放操作结果，请查询原请求。") from exc
        if getattr(record, "status", None) == "unknown":
            raise self._unknown(request_id, operation, "设备播放操作结果未知，请查询原请求。")
        if getattr(record, "status", None) != "succeeded":
            if getattr(record, "status", None) == "failed":
                raise self._unknown(request_id, operation, "设备报告播放操作失败，但不能确认副作用状态；请查询原请求。")
            raise self._unknown(request_id, operation, "设备回执状态无效，请查询原请求。")
        return request_id, getattr(record, "result", None)

    async def stop_and_snapshot_confirmed(self, key: str | None = None) -> PlaybackSnapshot:
        operation = "stop_and_snapshot"
        request_id, payload = await self._execute(operation, {"playback_key": key})
        try:
            return _snapshot_from_wire(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise self._unknown(request_id, operation, "设备停止回执无效，播放状态未知。") from exc

    async def pause_confirmed(self, key: str) -> bool:
        return await self._set_paused(key, True)

    async def resume_confirmed(self, key: str) -> bool:
        return await self._set_paused(key, False)

    async def _set_paused(self, key: str, paused: bool) -> bool:
        operation = "pause" if paused else "resume"
        request_id, payload = await self._execute(operation, {"playback_key": _identifier(key)})
        try:
            item = _mapping(payload)
            if set(item) != {"playback_key", "paused"} or item["playback_key"] != key \
                    or type(item["paused"]) is not bool or item["paused"] is not paused:
                raise ValueError("播放暂停/恢复回执与原播放键或期望状态不匹配")
            return True
        except (TypeError, ValueError, KeyError) as exc:
            raise self._unknown(request_id, operation, "设备播放暂停/恢复回执无效，状态未知。") from exc

    async def prepare_replacement_transaction(self, key: str | None, *, max_ms: int = 600) -> PlaybackReplacementTicket:
        operation = "prepare_replacement"
        if isinstance(max_ms, bool) or not isinstance(max_ms, int) or not 1 <= max_ms <= 600:
            raise ValueError("替换收尾时长必须为 1 至 600 毫秒。")
        request_id, payload = await self._execute(operation, {"playback_key": key, "max_ms": max_ms})
        try:
            item = _mapping(payload)
            if set(item) != {"replacement_id", "old_playback_key", "prepared_device_time_us", "snapshot"}:
                raise ValueError("替换票据字段不匹配")
            return PlaybackReplacementTicket(
                replacement_id=_identifier(item["replacement_id"]),
                old_key=_optional_identifier(item["old_playback_key"]),
                snapshot=_snapshot_from_wire(item["snapshot"]),
                prepared_device_time_us=_counter(item["prepared_device_time_us"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise self._unknown(request_id, operation, "设备替换票据无效，替换状态未知。") from exc

    async def bind_replacement_transaction(self, ticket: PlaybackReplacementTicket, key: str) -> bool:
        operation = "bind_replacement"
        request_id, payload = await self._execute(operation, {
            "replacement_id": _identifier(ticket.replacement_id), "playback_key": _identifier(key),
        })
        try:
            item = _mapping(payload)
            if set(item) != {"bound"} or type(item["bound"]) is not bool:
                raise ValueError("后继绑定回执无效")
            return item["bound"]
        except (TypeError, ValueError, KeyError) as exc:
            raise self._unknown(request_id, operation, "设备后继绑定回执无效。") from exc

    async def abort_replacement_transaction(
        self, ticket: PlaybackReplacementTicket, reason: str = "stopped",
    ) -> PlaybackReplacementResult:
        return await self._replacement_result("abort_replacement", ticket, {"reason": _identifier(reason)})

    async def wait_replacement_transaction(self, ticket: PlaybackReplacementTicket) -> PlaybackReplacementResult:
        return await self._replacement_result("wait_replacement", ticket, {})

    async def _replacement_result(self, operation: str, ticket: PlaybackReplacementTicket,
                                  extra: dict[str, Any]) -> PlaybackReplacementResult:
        arguments = {"replacement_id": _identifier(ticket.replacement_id), **extra}
        request_id, payload = await self._execute(operation, arguments)
        try:
            item = _mapping(payload)
            expected = {"replacement_id", "reason", "started_device_time_us", "finished_device_time_us",
                        "events_device_time_us", "attenuated_range_ms", "snapshot"}
            if set(item) != expected:
                raise ValueError("替换结算字段不匹配")
            events = _mapping(item["events_device_time_us"])
            event_times = {_identifier(name): _counter(value) for name, value in events.items()}
            attenuated = item["attenuated_range_ms"]
            if not isinstance(attenuated, (list, tuple)) or len(attenuated) != 2:
                raise ValueError("替换不确定区间无效")
            start, end = (_counter(value) for value in attenuated)
            if end < start:
                raise ValueError("替换不确定区间顺序无效")
            started_at = _counter(item["started_device_time_us"])
            finished_at = _counter(item["finished_device_time_us"])
            if finished_at < started_at:
                raise ValueError("替换设备时间顺序无效")
            if _identifier(item["replacement_id"]) != ticket.replacement_id:
                raise ValueError("替换回执与原事务编号不匹配")
            return PlaybackReplacementResult(
                replacement_id=ticket.replacement_id,
                snapshot=_snapshot_from_wire(item["snapshot"]),
                reason=_identifier(item["reason"]),
                started_device_time_us=started_at,
                finished_device_time_us=finished_at,
                events_device_time_us=event_times,
                attenuated_range_ms=(start, end),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise self._unknown(request_id, operation, "设备替换结算回执无效，结果未知。") from exc


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("设备回执不是对象")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError("设备回执编号无效")
    return value


def _optional_identifier(value: Any) -> str | None:
    return None if value is None else _identifier(value)


def _counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _SAFE_INTEGER:
        raise ValueError("设备回执整数无效")
    return value


def _snapshot_from_wire(value: Any) -> PlaybackSnapshot:
    item = _mapping(value)
    required = {"playback_key", "enqueued_ms", "played_ms", "buffered_ms", "output_estimated_ms",
                "started", "device_time_us", "precision"}
    if set(item) != required:
        raise ValueError("播放快照字段不匹配")
    if type(item["started"]) is not bool or item["precision"] not in _PRECISIONS:
        raise ValueError("播放快照状态或精度无效")
    device_time_us = _counter(item["device_time_us"])
    return PlaybackSnapshot(
        key=_optional_identifier(item["playback_key"]),
        enqueued_ms=_counter(item["enqueued_ms"]),
        played_ms=_counter(item["played_ms"]),
        buffered_ms=_counter(item["buffered_ms"]),
        output_estimated_ms=_counter(item["output_estimated_ms"]),
        started=item["started"],
        captured_at=device_time_us / 1_000_000,
        precision=item["precision"],
        device_output_time=None,
        device_time_us=device_time_us,
    )
