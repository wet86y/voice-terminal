"""Python 与 Rust 设备控制协议 v1 的 JSON（结构化数据）字段一致性。"""
import json

import pytest

from voice_terminal.devices.router import DeviceRequest, DeviceOutcomeUnknown
from voice_terminal.devices.wire import (MAX_CONTROL_BYTES, decode_device_control_receipt,
                                         encode_device_control_request)


def request(operation="playback.prepare_replacement", arguments=None):
    return DeviceRequest("request-1", "设备-B", "core-1", "request-1", 2, 3,
                         operation, arguments if arguments is not None else {
                             "playback_key": "playback-1", "max_ms": 600,
                         })


def receipt(**changes):
    value = {
        "protocol_version": 1,
        "device_id": "设备-B",
        "core_session_id": "core-1",
        "connection_generation": 2,
        "executor_generation": 3,
        "request_id": "request-1",
        "receipt_sequence": 7,
        "status": "completed",
        "precision": "device_time",
        "result": {
            "kind": "replacement_prepared",
            "value": {
                "replacement_id": "replacement-1",
                "old_playback_key": "playback-1",
                "prepared_device_time_us": 1_000_000,
                "snapshot": {
                    "playback_key": "playback-1", "enqueued_ms": 500, "played_ms": 200,
                    "buffered_ms": 300, "output_estimated_ms": 200, "started": True,
                    "device_time_us": 1_000_000, "precision": "device_time",
                },
            },
        },
        "error": None,
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_request_encoding_matches_rust_control_envelope():
    payload = json.loads(encode_device_control_request(request()))
    assert payload == {
        "protocol_version": 1,
        "device_id": "设备-B",
        "core_session_id": "core-1",
        "connection_generation": 2,
        "executor_generation": 3,
        "request_id": "request-1",
        "task_id": "request-1",
        "command": {"operation": "prepare_replacement",
                    "arguments": {"playback_key": "playback-1", "max_ms": 600}},
    }


def test_receipt_decoder_checks_core_device_and_task_generation_fields():
    response = decode_device_control_receipt(receipt(), request())
    assert response.succeeded is True
    assert response.core_session_id == "core-1"
    assert response.task_id == "request-1"
    assert response.result["replacement_id"] == "replacement-1"


@pytest.mark.parametrize(("operation", "paused"), [("playback.pause", True), ("playback.resume", False)])
def test_pause_resume_device_commands_and_confirmed_state_receipts(operation, paused):
    payload = json.loads(encode_device_control_request(request(operation, {"playback_key": "playback-1"})))
    assert payload["command"] == {"operation": operation.split(".", 1)[1],
                                  "arguments": {"playback_key": "playback-1"}}
    response = decode_device_control_receipt(receipt(result={
        "kind": "state", "value": {"playback_key": "playback-1", "paused": paused},
    }), request(operation, {"playback_key": "playback-1"}))
    assert response.succeeded is True
    assert response.result == {"playback_key": "playback-1", "paused": paused}


@pytest.mark.parametrize("changes", [
    {"core_session_id": "stale-core"},
    {"connection_generation": 1},
    {"executor_generation": 4},
    {"request_id": "other-request"},
])
def test_stale_receipts_are_unknown_not_success(changes):
    with pytest.raises(DeviceOutcomeUnknown):
        decode_device_control_receipt(receipt(**changes), request())


def test_unknown_receipt_and_duplicate_or_oversized_json_are_rejected_as_unknown():
    unknown = receipt(status="unknown", precision="unknown", result=None, error="设备未回执")
    with pytest.raises(DeviceOutcomeUnknown):
        decode_device_control_receipt(unknown, request())
    duplicate = b'{"protocol_version":1,"protocol_version":1}'
    with pytest.raises(DeviceOutcomeUnknown):
        decode_device_control_receipt(duplicate, request())
    with pytest.raises(DeviceOutcomeUnknown):
        decode_device_control_receipt(b" " * (MAX_CONTROL_BYTES + 1), request())
