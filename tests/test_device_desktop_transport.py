from __future__ import annotations

import pytest

from voice_terminal.devices.desktop_transport import LocalDesktopHelperTransport
from voice_terminal.devices.router import DeviceOutcomeUnknown, DeviceRequest, DeviceUnavailable


def request(device_id: str = "local") -> DeviceRequest:
    return DeviceRequest(
        request_id="task-1", device_id=device_id, core_session_id="core-1", task_id="task-1",
        connection_generation=3, executor_generation=5, operation="windows", arguments={},
    )


@pytest.mark.asyncio
async def test_local_desktop_transport_preserves_task_and_generation_identity():
    calls = []

    async def send(request_id, operation, arguments, timeout_ms, connection_generation, executor_generation):
        calls.append((request_id, operation, arguments, timeout_ms, connection_generation, executor_generation))
        return {"protocol_version": 1, "request_id": request_id, "ok": True, "result": {"windows": []}}

    transport = LocalDesktopHelperTransport(send)
    response = await transport.execute(request())
    assert calls == [("task-1", "windows", {}, 30_000, 3, 5)]
    assert response.request_id == "task-1"
    assert response.device_id == "local"
    assert response.core_session_id == "core-1"
    assert response.connection_generation == 3
    assert response.executor_generation == 5
    assert response.succeeded is True


@pytest.mark.asyncio
async def test_local_desktop_transport_does_not_retarget_or_retry_unknown_results():
    calls = []

    async def send(*args):
        calls.append(args)
        raise TimeoutError("unknown")

    transport = LocalDesktopHelperTransport(send)
    with pytest.raises(DeviceUnavailable):
        await transport.execute(request("remote-a"))
    assert calls == []
    with pytest.raises(DeviceOutcomeUnknown):
        await transport.execute(request())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_local_desktop_transport_rejects_mismatched_helper_receipt():
    async def send(*_args):
        return {"protocol_version": 1, "request_id": "another-task", "ok": True, "result": {}}

    transport = LocalDesktopHelperTransport(send)
    with pytest.raises(DeviceOutcomeUnknown):
        await transport.execute(request())


@pytest.mark.asyncio
async def test_local_desktop_batch_stops_on_first_failure_and_reports_completed_steps():
    calls = []

    async def send(request_id, operation, arguments, _timeout_ms, _connection_generation, _executor_generation):
        calls.append((request_id, operation, arguments))
        if request_id.endswith(":2"):
            return {"protocol_version": 1, "request_id": request_id, "ok": False,
                    "error": {"message": "element not found"}}
        return {"protocol_version": 1, "request_id": request_id, "ok": True,
                "result": {"action": operation}}

    action = request()
    action = DeviceRequest(
        request_id=action.request_id, device_id=action.device_id, core_session_id=action.core_session_id,
        task_id=action.task_id, connection_generation=action.connection_generation,
        executor_generation=action.executor_generation, operation="multi_action",
        arguments={"window_id": 99, "steps":[
            {"kind":"click","elementId":"1.2"}, {"kind":"type","elementId":"1.3","text":"hello"},
            {"kind":"click","x":20,"y":30},
        ], "continue_on_error":False},
    )
    response = await LocalDesktopHelperTransport(send).execute(action)
    assert response.succeeded is False
    assert [call[0] for call in calls] == ["task-1:1", "task-1:2"]
    assert calls[0][2]["request"]["windowId"] == 99
    assert response.result["completed_count"] == 1
    assert response.result["total_steps"] == 3
