"""Desktop helper transport（桌面辅助传输）：本机动作经 Tauri（桌面框架）受管进程执行。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .router import DeviceOutcomeUnknown, DeviceRequest, DeviceResponse, DeviceUnavailable

HelperRequest = Callable[[str, str, dict[str, Any], int, int, int], Awaitable[dict[str, Any]]]


class LocalDesktopHelperTransport:
    """LocalDesktopHelperTransport（本机桌面传输）：不读取凭据或回退到其他设备。"""

    def __init__(self, send: HelperRequest, *, device_id: str = "local", timeout_ms: int = 30_000) -> None:
        if not callable(send) or not isinstance(device_id, str) or not device_id or device_id != device_id.strip():
            raise ValueError("本机桌面传输入口或设备编号无效。")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 600_000:
            raise ValueError("本机桌面操作时限必须为 1 至 600000 毫秒。")
        self._send = send
        self.device_id = device_id
        self.timeout_ms = timeout_ms

    @staticmethod
    def _response(request: DeviceRequest, *, succeeded: bool, result: Any) -> DeviceResponse:
        return DeviceResponse(
            request_id=request.request_id,
            device_id=request.device_id,
            core_session_id=request.core_session_id,
            task_id=request.task_id,
            connection_generation=request.connection_generation,
            executor_generation=request.executor_generation,
            succeeded=succeeded,
            result=result,
        )

    async def _send_step(self, request: DeviceRequest, request_id: str, operation: str,
                         arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._send(request_id, operation, arguments, self.timeout_ms,
                                        request.connection_generation, request.executor_generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DeviceOutcomeUnknown("本机桌面操作未能取得回执，结果未知。") from exc
        if (not isinstance(response, dict)
                or response.get("protocol_version") != 1
                or response.get("request_id") != request_id
                or type(response.get("ok")) is not bool):
            raise DeviceOutcomeUnknown("本机桌面回执与原任务编号不匹配，结果未知。")
        return response

    async def _execute_batch(self, request: DeviceRequest) -> DeviceResponse:
        steps = request.arguments.get("steps")
        window_id = request.arguments.get("window_id")
        continue_on_error = request.arguments.get("continue_on_error", False)
        if (not isinstance(steps, list) or not 1 <= len(steps) <= 32
                or type(window_id) is not int or window_id <= 0 or type(continue_on_error) is not bool
                or len(request.task_id) > 120):
            raise DeviceUnavailable("本机批量动作参数无效。")
        completed: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("kind") not in {"click", "type"}:
                raise DeviceUnavailable("本机批量动作包含不支持的步骤。")
            helper_request: dict[str, Any] = {"windowId": window_id}
            helper_operation = step["kind"]
            if helper_operation == "click":
                for key in ("elementId", "x", "y", "button", "selectionMode"):
                    if key in step:
                        helper_request[key] = step[key]
                by_element = "elementId" in helper_request
                by_coordinates = "x" in helper_request and "y" in helper_request
                if by_element == by_coordinates:
                    raise DeviceUnavailable("批量点击必须指定 elementId（界面元素）或坐标 x/y。")
            else:
                if not isinstance(step.get("elementId"), str) or not isinstance(step.get("text"), str):
                    raise DeviceUnavailable("批量输入必须指定 elementId（界面元素）和文本。")
                helper_request.update(elementId=step["elementId"], text=step["text"], rollback=True)
            helper_arguments = {"request": helper_request, "focusFallback": False}
            helper_id = f"{request.task_id}:{index + 1}"
            helper_response = await self._send_step(request, helper_id, helper_operation, helper_arguments)
            if helper_response["ok"]:
                completed.append({"index": index, "ok": True, "result": helper_response.get("result")})
            else:
                completed.append({"index": index, "ok": False, "error": helper_response.get("error")})
                if not continue_on_error:
                    break
        succeeded = all(item["ok"] for item in completed) and len(completed) == len(steps)
        return self._response(request, succeeded=succeeded, result={
            "completed_steps": completed,
            "completed_count": sum(item["ok"] for item in completed),
            "total_steps": len(steps),
            "continued_after_failure": continue_on_error,
        })

    async def execute(self, request: DeviceRequest) -> DeviceResponse:
        """execute（执行）：使用原任务编号，迟到、畸形及断线结果均不重发。"""
        if request.device_id != self.device_id:
            raise DeviceUnavailable("本机桌面传输拒绝其他设备目标。")
        if not isinstance(request.arguments, dict):
            raise DeviceUnavailable("本机桌面操作参数必须是对象。")
        if request.operation == "multi_action":
            return await self._execute_batch(request)
        response = await self._send_step(request, request.task_id, request.operation, request.arguments)
        return self._response(request, succeeded=response["ok"],
                              result=response.get("result") if response["ok"] else response.get("error"))
