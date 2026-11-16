"""Device control wire format（设备控制线格式）：与 Rust（原生语言）协议 v1 严格对齐。"""
from __future__ import annotations

import json
from typing import Any

from .router import DeviceOutcomeUnknown, DeviceRequest, DeviceResponse

PROTOCOL_VERSION = 1  # PROTOCOL_VERSION（设备协议版本）。
MAX_CONTROL_BYTES = 16 * 1024  # MAX_CONTROL_BYTES（JSON 控制消息最大字节数）。
MAX_SAFE_INTEGER = (1 << 53) - 1  # MAX_SAFE_INTEGER（跨语言安全整数上限）。

_COMMAND_FIELDS = {
    "playback.pause": ("pause", {"playback_key"}),
    "playback.resume": ("resume", {"playback_key"}),
    "playback.stop_and_snapshot": ("stop_and_snapshot", {"playback_key"}),
    "playback.prepare_replacement": ("prepare_replacement", {"playback_key", "max_ms"}),
    "playback.bind_replacement": ("bind_replacement", {"replacement_id", "playback_key"}),
    "playback.abort_replacement": ("abort_replacement", {"replacement_id", "reason"}),
    "playback.wait_replacement": ("wait_replacement", {"replacement_id"}),
}


def encode_device_control_request(request: DeviceRequest) -> bytes:
    """encode_device_control_request（编码播放请求）：目标、代次及命令参数必须完整匹配。"""
    if not isinstance(request.operation, str):
        raise ValueError("设备控制操作不受支持。")
    command = _COMMAND_FIELDS.get(request.operation)
    if command is None:
        raise ValueError("设备控制操作不受支持。")
    operation, fields = command
    arguments = request.arguments
    if not isinstance(arguments, dict) or set(arguments) != fields:
        raise ValueError("设备控制参数字段不匹配。")
    for value in (request.device_id, request.core_session_id, request.task_id, request.request_id):
        _identifier(value)
    _counter(request.connection_generation, positive=True)
    _counter(request.executor_generation, positive=True)
    if request.task_id != request.request_id:
        raise ValueError("此版本要求持久任务编号与控制请求编号一致。")
    if operation in {"stop_and_snapshot", "prepare_replacement"}:
        _optional_identifier(arguments["playback_key"])
    if operation in {"pause", "resume"}:
        _identifier(arguments["playback_key"])
    if operation == "prepare_replacement" and (
            isinstance(arguments["max_ms"], bool) or not isinstance(arguments["max_ms"], int)
            or not 1 <= arguments["max_ms"] <= 600):
        raise ValueError("替换收尾时长无效。")
    if operation == "bind_replacement":
        _identifier(arguments["replacement_id"])
        _identifier(arguments["playback_key"])
    if operation == "abort_replacement":
        _identifier(arguments["replacement_id"])
        _identifier(arguments["reason"])
    if operation == "wait_replacement":
        _identifier(arguments["replacement_id"])
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "device_id": request.device_id,
        "core_session_id": request.core_session_id,
        "connection_generation": request.connection_generation,
        "executor_generation": request.executor_generation,
        "request_id": request.request_id,
        "task_id": request.task_id,
        "command": {"operation": operation, "arguments": arguments},
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_CONTROL_BYTES:
        raise ValueError("设备控制消息超过长度上限。")
    return encoded


def decode_device_control_receipt(data: bytes, request: DeviceRequest) -> DeviceResponse:
    """decode_device_control_receipt（解码设备回执）：错误代次与未知状态均不作为成功。"""
    if not isinstance(data, bytes) or not data or len(data) > MAX_CONTROL_BYTES:
        raise DeviceOutcomeUnknown("设备控制回执长度无效。")
    try:
        receipt = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        _exact_fields(receipt, {"protocol_version", "device_id", "core_session_id", "connection_generation",
                                "executor_generation", "request_id", "receipt_sequence", "status",
                                "precision", "result", "error"})
        if (receipt["protocol_version"] != PROTOCOL_VERSION
                or receipt["device_id"] != request.device_id
                or receipt["core_session_id"] != request.core_session_id
                or receipt["connection_generation"] != request.connection_generation
                or receipt["executor_generation"] != request.executor_generation
                or receipt["request_id"] != request.request_id):
            raise ValueError("回执与原核心、设备、请求或代次不匹配")
        _counter(receipt["receipt_sequence"], positive=True)
        if receipt["precision"] not in {"callback", "device_time", "estimated", "unknown"}:
            raise ValueError("回执精度编号无效")
        status = receipt["status"]
        if status == "unknown":
            if receipt["precision"] != "unknown" or receipt["result"] is not None:
                raise ValueError("未知回执不得伪装为已确认状态")
            message = _optional_text(receipt["error"], max_chars=512)
            raise DeviceOutcomeUnknown(message or "设备回执明确报告结果未知。")
        if status == "accepted":
            raise DeviceOutcomeUnknown("设备只报告已受理，尚未确认操作完成。")
        if status == "rejected":
            _optional_text(receipt["error"], max_chars=512)
            if receipt["result"] is not None:
                raise ValueError("拒绝回执不得包含成功结果")
            return DeviceResponse(request.request_id, request.device_id, request.core_session_id,
                                  request.task_id, request.connection_generation,
                                  request.executor_generation, False, {"error": receipt["error"]})
        if status != "completed" or receipt["result"] is None:
            raise ValueError("设备回执状态或完成结果无效")
        result = _validate_control_result(receipt["result"])
        return DeviceResponse(request.request_id, request.device_id, request.core_session_id,
                              request.task_id, request.connection_generation,
                              request.executor_generation, True, result)
    except DeviceOutcomeUnknown:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise DeviceOutcomeUnknown("设备回执无法验证，请查询原任务。") from exc


def _validate_control_result(value: Any) -> dict[str, Any]:
    _exact_fields(value, {"kind", "value"})
    kind, result = value["kind"], value["value"]
    if kind == "state":
        _exact_fields(result, {"playback_key", "paused"})
        _identifier(result["playback_key"])
        if type(result["paused"]) is not bool:
            raise ValueError("播放暂停状态无效")
        return result
    if kind == "snapshot":
        _snapshot(result)
        return result
    if kind == "replacement_prepared":
        _exact_fields(result, {"replacement_id", "old_playback_key", "prepared_device_time_us", "snapshot"})
        _identifier(result["replacement_id"])
        _optional_identifier(result["old_playback_key"])
        _counter(result["prepared_device_time_us"])
        _snapshot(result["snapshot"])
        return result
    if kind == "replacement":
        _exact_fields(result, {"replacement_id", "reason", "started_device_time_us", "finished_device_time_us",
                               "events_device_time_us", "attenuated_range_ms", "snapshot"})
        _identifier(result["replacement_id"])
        _identifier(result["reason"])
        started, finished = (_counter(result[name]) for name in
                             ("started_device_time_us", "finished_device_time_us"))
        if finished < started:
            raise ValueError("替换回执时序倒置")
        events = result["events_device_time_us"]
        if not isinstance(events, dict):
            raise ValueError("替换事件不是对象")
        for name, value in events.items():
            _identifier(name)
            _counter(value)
        attenuated = result["attenuated_range_ms"]
        if not isinstance(attenuated, list) or len(attenuated) != 2:
            raise ValueError("旧音不确定区间无效")
        attenuated = [_counter(value) for value in attenuated]
        if attenuated[1] < attenuated[0]:
            raise ValueError("旧音不确定区间顺序无效")
        _snapshot(result["snapshot"])
        return result
    raise ValueError("设备回执结果类型不受支持")


def _snapshot(value: Any) -> None:
    _exact_fields(value, {"playback_key", "enqueued_ms", "played_ms", "buffered_ms", "output_estimated_ms",
                          "started", "device_time_us", "precision"})
    _optional_identifier(value["playback_key"])
    for name in ("enqueued_ms", "played_ms", "buffered_ms", "output_estimated_ms", "device_time_us"):
        _counter(value[name])
    if type(value["started"]) is not bool:
        raise ValueError("播放启动状态无效")
    if value["precision"] not in {"callback", "device_time", "estimated", "unknown"}:
        raise ValueError("播放快照精度无效")


def _exact_fields(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("设备控制对象字段不匹配")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("设备控制 JSON 不允许重复字段")
        result[key] = value
    return result


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError("设备控制编号无效")
    return value


def _optional_identifier(value: Any) -> str | None:
    return None if value is None else _identifier(value)


def _optional_text(value: Any, *, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_chars or value != value.strip():
        raise ValueError("设备错误文本无效")
    return value


def _counter(value: Any, *, positive: bool = False) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value > MAX_SAFE_INTEGER
            or value < (1 if positive else 0)):
        raise ValueError("设备控制整数超出有效范围")
    return value
