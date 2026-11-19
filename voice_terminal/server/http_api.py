"""HTTP and WebSocket routes for the session runtime."""
from __future__ import annotations
from dataclasses import replace

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote
from uuid import uuid4

from .. import __version__  # __version__（后端源码版本）：接口元数据与发布记录共用。
from ..core.events import VoiceEvent, event
from ..dsh import BRIDGE_PROTOCOL_VERSION, DshBridgeError, DshShadowAgentSession
from ..application.ports import validate_endpoint_url
from ..devices.clock_sync import DeviceClockMapper, MediaClockSync, media_clock
from ..devices.diagnostics import device_event
from ..devices.media_protocol import (
    PROTOCOL_VERSION as MEDIA_PROTOCOL_VERSION, MediaProtocolError, decode_client_tts_audio_frame,
    decode_frame, parse_tts_control,
)
from ..devices.pcm_stream import PcmInputStream
from ..devices.remote_capture import RemoteAudioCapture
from ..devices.remote_playback import RemotePlaybackOutbox, encode_playback_frame
from ..devices.router import DeviceOutcomeUnknown, DeviceTargetChanged, DeviceUnavailable
from ..devices.task_journal import TaskConflict
from ..devices.client_tts import RemoteClientTtsOutbox
from ..speaker.enrollment import EnrollmentRejectedError
from ..speaker.enrollment_capture import EnrollmentCaptureError
from ..speaker.audit import audit as speaker_audit, parameter_snapshot
from ..model_registry.service import ModelRegistryError, ModelRegistryService
from ..model_registry.vault import CredentialVaultUnavailable
from ..devices.trust import DeviceTrustError
from .product import (
    PRODUCT_MINIMAX_TTS_MODELS, ProductSettingsError, ProductSettingsStore,
    _threshold_to_sensitivity, sanitize_runtime_snapshot,
)
from .snapshot import ProtocolSequencer, snapshot_event
from .runtime_operations import runtime_operation, runtime_operations

logger = logging.getLogger(__name__)


def device_file_subtask_id(request_id: str, suffix: str) -> str:
    """device_file_subtask_id（文件子任务编号）：为重试固定到同一设备台账记录。"""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8]
    return f"{request_id[:105]}:{suffix}:{digest}"


def device_file_transfer_id(connection: Any, request_id: str) -> str:
    """device_file_transfer_id（文件传输编号）：绑定原请求和设备连接/执行器代次。"""
    material = (f"{connection.device_id}\0{connection.connection_generation}\0"
                f"{connection.executor_generation}\0{request_id}").encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


# DEVICE_MANAGEMENT_OPERATIONS（受限远程业务操作）：每个枚举只映射到本服务既有业务路由。
DEVICE_MANAGEMENT_OPERATIONS: dict[str, tuple[str, str, bool]] = {
    "product.snapshot": ("GET", "/api/v1/product/snapshot", False),
    "product.devices": ("GET", "/api/v1/devices", False),
    "product.devices.select": ("POST", "/api/v1/devices/interaction", True),
    "product.compute_status": ("GET", "/api/v1/product/compute-status", False),
    "product.compute_status.refresh": ("GET", "/api/v1/product/compute-status?refresh=true", False),
    "product.dsh_status": ("GET", "/api/v1/dsh/status", False),
    "product.wake_tasks": ("GET", "/api/v1/wake-tasks", False),
    "settings.hotwords.read": ("GET", "/api/v1/product/settings/hotwords", False),
    "settings.hotwords.save": ("PUT", "/api/v1/product/settings/hotwords", True),
    "hotwords.import": ("POST", "/api/v1/product/hotwords/import", True),
    "hotwords.preview": ("POST", "/api/v1/product/hotwords/preview", True),
    "settings.wake.save": ("PUT", "/api/v1/product/settings/wake", True),
    "settings.silence.save": ("PUT", "/api/v1/product/settings/silence", True),
    "settings.quiet_hours.save": ("PUT", "/api/v1/product/settings/quiet-hours", True),
    "settings.shadow.save": ("PUT", "/api/v1/product/settings/shadow", True),
    "settings.approvals.save": ("PUT", "/api/v1/product/settings/approvals", True),
    "settings.persona.save": ("PUT", "/api/v1/product/settings/persona", True),
    "settings.custom_instructions.save": ("PUT", "/api/v1/product/settings/custom-instructions", True),
    "settings.speaker.save": ("PUT", "/api/v1/product/settings/speaker", True),
    "settings.compute.save": ("PUT", "/api/v1/product/settings/compute", True),
    "settings.audio_enhancements.save": ("PUT", "/api/v1/product/settings/audio-enhancements", True),
    "settings.screen_capture.save": ("PUT", "/api/v1/product/settings/screen-capture", True),
    "settings.tts.save": ("PUT", "/api/v1/product/settings/tts", True),
    "settings.inference.save": ("PUT", "/api/v1/settings/inference", True),
    "models.registry.read": ("GET", "/api/v1/models/registry", False),
    "models.scan": ("POST", "/api/v1/models/scan", False),
    "models.validate": ("POST", "/api/v1/models/validate", True),
    "models.providers.add": ("POST", "/api/v1/models/providers", True),
    "models.assignments.apply": ("POST", "/api/v1/models/assignments/apply", True),
    "models.credentials.read": ("GET", "/api/v1/models/credentials", False),
    "models.credentials.save": ("PUT", "/api/v1/models/credentials", True),
    "tts.status": ("GET", "/api/v1/tts/status", False),
    "speaker.status": ("GET", "/api/v1/speaker/status", False),
    "speaker.profile": ("GET", "/api/v1/speaker/profile", False),
    "speaker.enrollment.start": ("POST", "/api/v1/speaker/enrollment/start", False),
    "speaker.test.capture": ("POST", "/api/v1/speaker/test/capture", True),
    "speaker.test.capture_stop": ("POST", "/api/v1/speaker/test/capture/stop", True),
    "speaker.test.run": ("POST", "/api/v1/speaker/test", True),
    "tts.minimax.voice_name": ("POST", "/api/v1/tts/minimax/voice-name", True),
    "tts.operation.preview": ("POST", "/api/v1/tts/operations/preview", True),
    "tts.operation.minimax_preview": ("POST", "/api/v1/tts/operations/minimax-preview", True),
    "tts.operation.play": ("POST", "/api/v1/tts/operations/play", True),
    "tts.operation.transcribe": ("POST", "/api/v1/tts/operations/transcribe", True),
    "tts.aniflive.import.pending": ("GET", "/api/v1/tts/aniflive/import/pending", False),
    "tts.aniflive.import.scan": ("POST", "/api/v1/tts/aniflive/import/scan", True),
    "tts.aniflive.import.start": ("POST", "/api/v1/tts/aniflive/import/jobs", True),
    "tts.assets.import.prepare": ("POST", "/internal/application/tts-assets/import/prepare", False),
    "tts.assets.import.commit": ("POST", "/internal/application/tts-assets/import/commit", False),
    "tts.assets.import.abort": ("POST", "/internal/application/tts-assets/import/abort", False),
    "tts.assets.audio.read": ("POST", "/internal/application/tts-assets/audio/read", True),
    "dsh.approval.decide": ("POST", "/internal/application/dsh-approval", True),
}


def _remote_management_request_spec(
    operation: str, arguments: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None]:
    """_remote_management_request_spec（固定操作解析）：返回既有路由，不接收任意 URL。"""
    tts_import_fields = {
        "tts.assets.import.prepare": {"model_id", "purpose", "filename", "expected_bytes", "parent_request_id"},
        "tts.assets.import.commit": {"model_id", "purpose", "filename", "expected_bytes", "expected_sha256",
                                     "parent_request_id", "transfer_id"},
        "tts.assets.import.abort": {"parent_request_id", "transfer_id"},
    }
    if operation in tts_import_fields:
        if set(arguments) != tts_import_fields[operation]:
            raise ValueError("TTS 单文件导入操作字段不符合固定票据契约。")
        return DEVICE_MANAGEMENT_OPERATIONS[operation][0], DEVICE_MANAGEMENT_OPERATIONS[operation][1], dict(arguments)
    if operation == "tts.assets.audio.read":
        value = arguments.get("value")
        if (set(arguments) != {"value"} or not isinstance(value, dict)
                or set(value) != {"model_id", "asset_id"}):
            raise ValueError("TTS 音频读取只接受 value 中的 model_id 与 asset_id。")
        return DEVICE_MANAGEMENT_OPERATIONS[operation][0], DEVICE_MANAGEMENT_OPERATIONS[operation][1], dict(value)
    if operation == "dsh.approval.decide":
        if set(arguments) != {"approval_id", "decision", "run_seq"}:
            raise ValueError("DSH 权限答复只接受 approval_id、decision 与 run_seq。")
        approval_id = arguments.get("approval_id")
        decision = arguments.get("decision")
        run_seq = arguments.get("run_seq")
        if (not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 160
                or approval_id != approval_id.strip() or "\0" in approval_id
                or not isinstance(decision, str)
                or decision not in {"allowed-once", "allowed-until-silence", "rejected"}
                or type(run_seq) is not int or not 0 <= run_seq <= (1 << 53) - 1):
            raise ValueError("DSH 权限答复编号、决定或运行序号无效。")
        return DEVICE_MANAGEMENT_OPERATIONS[operation][0], DEVICE_MANAGEMENT_OPERATIONS[operation][1], dict(arguments)
    spec = DEVICE_MANAGEMENT_OPERATIONS.get(operation)
    if spec is not None:
        method, path, needs_value = spec
        if needs_value:
            if set(arguments) != {"value"} or not isinstance(arguments["value"], dict):
                raise ValueError("远程业务操作要求一个 JSON 对象参数。")
            value = arguments["value"]
            if operation == "tts.aniflive.import.scan":
                source_type = value.get("source_type")
                if source_type == "local":
                    raise ValueError("客户端本机目录须经受授权文件传输上传；远程接口不接收客户端路径。")
                if source_type not in {"huggingface", "modelscope"}:
                    raise ValueError("远程模型来源类型无效。")
            return method, path, value
        if arguments:
            raise ValueError("远程业务读取操作不接受额外参数。")
        return method, path, None

    def segment(key: str) -> str:
        value = arguments.get(key)
        if (not isinstance(value, str) or not value or len(value) > 128
                or value != value.strip() or value in {".", ".."}
                or not value.isascii()
                or any(not (character.isalnum() or character in "._:-") for character in value)):
            raise ValueError("远程业务资源编号无效。")
        return quote(value, safe="")

    # wake_task.create（创建唤醒任务）：严格接收服务端原宿主 API 的 value 对象。
    if operation == "wake_task.create":
        if set(arguments) != {"value"} or not isinstance(arguments["value"], dict):
            raise ValueError("创建定时任务必须只提供一个 JSON 对象参数 value。")
        return "POST", "/internal/application/wake-tasks", arguments["value"]
    if operation == "wake_task.update" and set(arguments) == {"task_id", "value"}:
        task_id = segment("task_id")
        if not isinstance(arguments["value"], dict):
            raise ValueError("定时任务更新内容必须是 JSON 对象。")
        return "PATCH", f"/api/v1/wake-tasks/{task_id}", arguments["value"]
    if operation in {"wake_task.delete", "wake_task.cancel"} and set(arguments) == {"task_id"}:
        task_id = segment("task_id")
        if operation == "wake_task.delete":
            return "DELETE", f"/api/v1/wake-tasks/{task_id}", None
        return "POST", f"/api/v1/wake-tasks/{task_id}/cancel", {}
    if operation == "provider.usage" and set(arguments) == {"provider"}:
        provider = arguments.get("provider")
        if not isinstance(provider, str) or provider not in {"deepseek", "minimax"}:
            raise ValueError("模型服务提供方编号无效。")
        return "GET", f"/api/v1/models/credentials/{provider}/usage", None
    if operation in {"model.update", "model.delete", "model.hide", "model.restore"}:
        expected = {"model_id", "value"} if operation == "model.update" else {"model_id"}
        if set(arguments) != expected:
            raise ValueError("模型操作参数字段无效。")
        model_id = segment("model_id")
        if operation == "model.update":
            if not isinstance(arguments["value"], dict):
                raise ValueError("模型更新内容必须是 JSON 对象。")
            return "PATCH", f"/api/v1/models/{model_id}", arguments["value"]
        if operation == "model.delete":
            return "DELETE", f"/api/v1/models/{model_id}", None
        action = "hide" if operation == "model.hide" else "restore"
        return "POST", f"/api/v1/models/{model_id}/{action}", {}
    if operation == "tts.voices.read" and set(arguments) == {"model_id"}:
        return "GET", f"/api/v1/tts/voices/{segment('model_id')}", None
    if operation == "tts.voices.capabilities" and set(arguments) == {"model_id"}:
        return "GET", f"/api/v1/tts/voices/{segment('model_id')}/capabilities", None
    if operation in {"tts.voices.create", "tts.voices.select"} and set(arguments) == {"model_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("语音音色参数必须是 JSON 对象。")
        model_id = segment("model_id")
        if operation == "tts.voices.create":
            return "POST", f"/api/v1/tts/voices/{model_id}", arguments["value"]
        return "PUT", f"/api/v1/tts/voices/{model_id}/selection", arguments["value"]
    if operation in {"tts.voices.update", "tts.voices.delete"}:
        expected = {"model_id", "voice_id", "value"} if operation == "tts.voices.update" else {"model_id", "voice_id"}
        if set(arguments) != expected:
            raise ValueError("语音音色目标参数无效。")
        model_id = segment("model_id")
        voice_id = segment("voice_id")
        if operation == "tts.voices.update":
            if not isinstance(arguments["value"], dict):
                raise ValueError("语音音色更新内容必须是 JSON 对象。")
            return "PUT", f"/api/v1/tts/voices/{model_id}/{voice_id}", arguments["value"]
        return "DELETE", f"/api/v1/tts/voices/{model_id}/{voice_id}", None
    if operation == "tts.voices.restore_default" and set(arguments) == {"model_id"}:
        return "POST", f"/api/v1/tts/voices/{segment('model_id')}/restore-default", {}
    if operation == "tts.voices.default_reference" and set(arguments) == {"model_id", "voice_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("默认参考录音参数必须是 JSON 对象。")
        model_id = segment("model_id")
        voice_id = segment("voice_id")
        return "PUT", f"/api/v1/tts/voices/{model_id}/{voice_id}/default-reference", arguments["value"]
    if operation == "tts.assets.clip" and set(arguments) == {"model_id", "asset_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("音频裁剪参数必须是 JSON 对象。")
        model_id = segment("model_id")
        asset_id = segment("asset_id")
        return "POST", f"/api/v1/tts/assets/{model_id}/{asset_id}/clip", arguments["value"]
    if operation == "tts.operation.read" and set(arguments) == {"operation_id"}:
        return "GET", f"/api/v1/tts/operations/{segment('operation_id')}", None
    if operation == "tts.operation.cancel" and set(arguments) == {"operation_id"}:
        return "POST", f"/api/v1/tts/operations/{segment('operation_id')}/cancel", {}
    if operation == "tts.aniflive.references.import" and set(arguments) == {"model_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("参考录音导入参数必须是 JSON 对象。")
        return "POST", f"/api/v1/tts/aniflive/models/{segment('model_id')}/references/import", arguments["value"]
    if operation == "tts.aniflive.model.rename" and set(arguments) == {"model_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("AnifLive 模型重命名参数必须是 JSON 对象。")
        return "PATCH", f"/api/v1/tts/aniflive/models/{segment('model_id')}", arguments["value"]
    if operation == "tts.aniflive.import.read" and set(arguments) == {"task_id"}:
        return "GET", f"/api/v1/tts/aniflive/import/jobs/{segment('task_id')}", None
    if operation == "tts.aniflive.import.reference" and set(arguments) == {"task_id", "value"}:
        if not isinstance(arguments["value"], dict):
            raise ValueError("参考转写内容必须是 JSON 对象。")
        return "POST", f"/api/v1/tts/aniflive/import/jobs/{segment('task_id')}/reference", arguments["value"]
    if operation in {
        "speaker.enrollment.segment", "speaker.enrollment.capture",
        "speaker.enrollment.capture_stop", "speaker.enrollment.commit", "speaker.enrollment.delete",
    }:
        method_by_operation = {
            "speaker.enrollment.segment": ("POST", "segment", True),
            "speaker.enrollment.capture": ("POST", "capture", True),
            "speaker.enrollment.capture_stop": ("POST", "capture/stop", True),
            "speaker.enrollment.commit": ("POST", "commit", False),
            "speaker.enrollment.delete": ("DELETE", "", False),
        }
        method, suffix, needs_value = method_by_operation[operation]
        expected = {"enrollment_id", "value"} if needs_value else {"enrollment_id"}
        if set(arguments) != expected or (needs_value and not isinstance(arguments["value"], dict)):
            raise ValueError("声纹登记操作参数字段无效。")
        path = f"/api/v1/speaker/enrollment/{segment('enrollment_id')}"
        if suffix:
            path += f"/{suffix}"
        return method, path, arguments["value"] if needs_value else None
    if operation in {"speaker.template.delete", "speaker.template.update"}:
        expected = {"template_id", "value"} if operation == "speaker.template.update" else {"template_id"}
        if set(arguments) != expected:
            raise ValueError("声纹模板操作字段无效。")
        path = f"/api/v1/speaker/templates/{segment('template_id')}"
        if operation == "speaker.template.delete":
            return "DELETE", path, None
        if not isinstance(arguments["value"], dict):
            raise ValueError("声纹模板更新参数必须是 JSON 对象。")
        return "PATCH", path, arguments["value"]
    raise ValueError("远程业务操作不在服务端固定白名单中。")

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # type: ignore
    from fastapi.responses import JSONResponse, Response  # type: ignore
except ImportError:  # pragma: no cover - optional during doctor/setup
    FastAPI = None  # type: ignore[assignment]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    HTTPException = RuntimeError  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    JSONResponse = Any  # type: ignore[assignment,misc]
    Response = Any  # type: ignore[assignment,misc]


# _GATEWAY_WEBSOCKET_PATHS（回环网关放行的 WebSocket 路径）：媒体通道与语音控制通道；
# 其余 WebSocket 一律以 1008（策略违规）关闭，避免网关端口变成通用业务入口。
_GATEWAY_WEBSOCKET_PATHS = frozenset({"/v1/media", "/ws"})


class LoopbackDeviceGatewayGuard:
    """LoopbackDeviceGatewayGuard（回环网关路径白名单）：18765 只暴露登记的轻量端点。"""

    def __init__(self, app: Any, *, gateway_port: int, allowed_paths: frozenset[str]) -> None:
        self.app = app
        self.gateway_port = gateway_port
        self.allowed_paths = allowed_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        server = scope.get("server")
        if isinstance(server, (tuple, list)) and len(server) == 2 and server[1] == self.gateway_port:
            if scope.get("type") != "http":
                if scope.get("type") == "websocket":
                    path = scope.get("path")
                    if path in _GATEWAY_WEBSOCKET_PATHS and path in self.allowed_paths:
                        await self.app(scope, receive, send)
                    else:
                        await send({"type": "websocket.close", "code": 1008})
                return
            if scope.get("method") != "POST" or scope.get("path") not in self.allowed_paths:
                response = JSONResponse({"detail": "设备回环网关路径未开放。"}, status_code=404)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _device_gateway_json(request: Any, *, label: str, max_bytes: int = 4096) -> dict[str, Any]:
    """_device_gateway_json（设备网关 JSON）：有界读取并拒绝重复字段。"""
    try:
        declared_length = request.headers.get("content-length")
        if declared_length is not None and int(declared_length) > max_bytes:
            raise HTTPException(status_code=413, detail=f"{label}超过大小上限。")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail=f"{label}超过大小上限。")
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks), object_pairs_hook=_unique_json_fields)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{label}不是有效且唯一字段的 JSON。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail=f"{label}必须是 JSON 对象。")
    return payload


async def _device_gateway_payload(request: Any) -> dict[str, Any]:
    """_device_gateway_payload（设备网关请求体）：仅接受版本字段。"""
    payload = await _device_gateway_json(request, label="设备网关请求")
    if (not isinstance(payload, dict) or set(payload) != {"protocol_version"}
            or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1):
        raise HTTPException(status_code=422, detail="设备网关协议版本或字段无效。")
    return payload


_PRODUCT_CONVERSATION_ENTRY_LIMIT = 200  # _PRODUCT_CONVERSATION_ENTRY_LIMIT（远程产品快照最多保留的服务器对话条数）。
_PRODUCT_CONVERSATION_BYTES_LIMIT = 1_048_576  # _PRODUCT_CONVERSATION_BYTES_LIMIT（对话快照编码上限，字节）。
_PRODUCT_CONVERSATION_TEXT_LIMIT = 4_000  # _PRODUCT_CONVERSATION_TEXT_LIMIT（单条对话及当前字幕字符上限）。
_PRODUCT_DELIVERY_TEXT_LIMIT = 1_000  # _PRODUCT_DELIVERY_TEXT_LIMIT（播报交付摘要文本字符上限）。
_PRODUCT_DELIVERY_TEXT_FIELDS = frozenset({"heard_text", "uncertain_text", "unheard_text"})
_PRODUCT_DELIVERY_LABEL_FIELDS = frozenset({"status", "termination", "precision", "playback_precision"})
_PRODUCT_DELIVERY_NUMBER_FIELDS = frozenset({
    "played_ms", "callback_played_ms", "output_tail_estimated_ms", "speech_onset_ms",
    "interrupt_confirmed_ms", "speech_onset_to_confirm_ms", "confirm_to_stop_ms",
    "playback_stop_ms", "cancel_confirmed_ms",
})


def _product_conversation_snapshot(
    conversation: Any, *, runtime_current: Any = None,
) -> dict[str, Any]:
    """_product_conversation_snapshot（产品对话投影）：给轮询端稳定的次序、当前字幕和有界内容。"""
    if not isinstance(conversation, Mapping):
        return {"recent": [], "entries": [], "latest_seq": 0, "current": {}}

    latest_seq = conversation.get("latest_seq")
    if type(latest_seq) is not int or not 0 <= latest_seq <= (1 << 53) - 1:
        latest_seq = 0

    def text(value: Any, limit: int) -> str:
        return value.replace("\0", "")[:limit] if isinstance(value, str) else ""

    recent: list[dict[str, Any]] = []
    raw_recent = conversation.get("recent")
    if isinstance(raw_recent, list):
        for item in raw_recent[-10:]:
            if not isinstance(item, Mapping):
                continue
            recent.append({
                "turn_id": text(item.get("turn_id"), 128),
                "transcript": text(item.get("transcript"), _PRODUCT_CONVERSATION_TEXT_LIMIT),
                "response": text(item.get("response"), _PRODUCT_CONVERSATION_TEXT_LIMIT),
                "completed_at": text(item.get("completed_at"), 80),
                "status": text(item.get("status"), 64),
            })

    current = dict(conversation.get("current")) if isinstance(conversation.get("current"), Mapping) else {}
    for name in ("partial_text", "final_text", "assistant_text", "tts_text"):
        current[name] = text(current.get(name), _PRODUCT_CONVERSATION_TEXT_LIMIT)
    for name, limit in (("tts_status", 80), ("admission_phase", 80)):
        if name in current:
            current[name] = text(current[name], limit)
    raw_current = runtime_current if isinstance(runtime_current, Mapping) else {}
    current_turn_id = raw_current.get("turn_id")
    if current_turn_id is None or isinstance(current_turn_id, str):
        current["turn_id"] = text(current_turn_id, 128) if current_turn_id is not None else None  # turn_id（当前语音输入轮次标识）。
    output_generation = raw_current.get("output_generation")
    if type(output_generation) is int and 0 <= output_generation <= (1 << 53) - 1:
        current["output_generation"] = output_generation  # output_generation（当前输出修订代次）。

    allowed_kinds = {"user_turn", "assistant_speech", "system_speech", "screen_capture"}
    entries: list[dict[str, Any]] = []
    previous_seq = 0
    raw_entries = conversation.get("entries")
    if isinstance(raw_entries, list):
        for item in raw_entries:
            if not isinstance(item, Mapping):
                continue
            sequence = item.get("seq")
            kind = item.get("kind")
            conversation_id = item.get("conversation_id")
            if (type(sequence) is not int or not previous_seq < sequence <= latest_seq
                    or not isinstance(kind, str) or kind not in allowed_kinds
                    or not isinstance(conversation_id, str) or not conversation_id):
                continue
            delivery_value = item.get("delivery")
            delivery_source = delivery_value if isinstance(delivery_value, Mapping) else {}
            delivery: dict[str, Any] = {}
            for name in _PRODUCT_DELIVERY_LABEL_FIELDS:
                if isinstance(delivery_source.get(name), str):
                    delivery[name] = text(delivery_source[name], 80)
            for name in _PRODUCT_DELIVERY_TEXT_FIELDS:
                if isinstance(delivery_source.get(name), str):
                    delivery[name] = text(delivery_source[name], _PRODUCT_DELIVERY_TEXT_LIMIT)
            for name in _PRODUCT_DELIVERY_NUMBER_FIELDS:
                value = delivery_source.get(name)
                if (value is None or type(value) in (int, float)
                        and 0 <= value <= (1 << 53) - 1 and math.isfinite(value)):
                    if name in delivery_source:
                        delivery[name] = value
            entries.append({
                "seq": sequence,  # seq（服务器分配的单调对话序号）。
                "kind": kind,
                "conversation_id": text(conversation_id, 128),
                "turn_id": text(item.get("turn_id"), 128),
                "speech_id": text(item.get("speech_id"), 128),
                "text": text(item.get("text"), _PRODUCT_CONVERSATION_TEXT_LIMIT),
                "status": text(item.get("status"), 80),
                "termination": text(item.get("termination"), 80),
                "delivery": delivery,
                "at": text(item.get("at"), 80),
            })
            previous_seq = sequence
    entries = entries[-_PRODUCT_CONVERSATION_ENTRY_LIMIT:]
    projection = {"recent": recent, "entries": entries, "latest_seq": latest_seq, "current": current}

    # The product snapshot is fetched through a 2 MiB management response cap.
    # Keep the ordered tail that fits a smaller budget so a long conversation
    # cannot make remote status polling fail; latest_seq remains the server head.
    def encoded_size(value: Any) -> int:
        return len(json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))

    entry_sizes = [encoded_size(entry) for entry in entries]
    current_size = encoded_size({**projection, "entries": []})
    current_size += sum(entry_sizes) + max(0, len(entries) - 1)
    while entries and current_size > _PRODUCT_CONVERSATION_BYTES_LIMIT:
        current_size -= entry_sizes.pop(0) + (1 if len(entries) > 1 else 0)
        entries.pop(0)
    recent_sizes = [encoded_size(item) for item in recent]
    current_size = encoded_size({**projection, "recent": []})
    current_size += sum(recent_sizes) + max(0, len(recent) - 1)
    while recent and current_size > _PRODUCT_CONVERSATION_BYTES_LIMIT:
        current_size -= recent_sizes.pop(0) + (1 if len(recent) > 1 else 0)
        recent.pop(0)
    if encoded_size(projection) > _PRODUCT_CONVERSATION_BYTES_LIMIT:
        # Defensive fallback for malformed historical speaker projections.
        bounded_current = {
            name: current[name]
            for name in ("partial_text", "final_text", "assistant_text", "tts_text", "tts_status", "turn_id", "output_generation")
            if name in current
        }
        projection["current"] = bounded_current
    return projection


def _unique_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """_unique_json_fields（唯一 JSON 字段）：拒绝配对签名输入中的重复键。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON 字段重复。")
        value[key] = item
    return value


async def _device_gateway_challenge_payload(request: Any) -> dict[str, Any]:
    """_device_gateway_challenge_payload（设备挑战请求）：绑定设备、方法与固定路径。"""
    payload = await _device_gateway_json(request, label="设备签名挑战")
    expected = {"protocol_version", "device_id", "method", "path"}
    method, path = payload.get("method"), payload.get("path")
    allowed_post_paths = {
        "/v1/status", "/v1/devices", "/v1/session/open", "/v1/session/poll",
        "/v1/session/complete", "/v1/session/reconcile", "/v1/session/heartbeat", "/v1/session/close",
        "/v1/interaction/prepare", "/v1/interaction/abort", "/v1/management", "/v1/file-transfer",
    }
    if (set(payload) != expected or type(payload.get("protocol_version")) is not int
            or payload.get("protocol_version") != 1
            or not isinstance(payload.get("device_id"), str)
            or not ((method == "POST" and path in allowed_post_paths)
                    or (method == "GET" and path == "/v1/media"))):
        raise HTTPException(status_code=422, detail="设备签名挑战字段或协议版本无效。")
    return payload


def _require_device_gateway_port(request: Any) -> None:
    """_require_device_gateway_port（设备网关端口）：禁止业务端点暴露在普通 HTTP 端口。"""
    server = request.scope.get("server")
    if not isinstance(server, (tuple, list)) or len(server) != 2 or server[1] != 18_765:
        raise HTTPException(status_code=404, detail="设备业务请求只允许通过受限 SSH 回环网关。")


def _authorize_device_gateway_request(
    runtime: Any, request: Any, payload: dict[str, Any], *, method: str = "POST",
) -> None:
    """_authorize_device_gateway_request（设备请求授权）：验证一次性挑战与受信 Ed25519 签名。"""
    authenticator = getattr(runtime, "device_request_authenticator", None)
    trust_store = getattr(runtime, "device_trust_store", None)
    if (authenticator is None or trust_store is None or trust_store.state != "valid"):
        raise HTTPException(status_code=503, detail="主机设备信任服务不可用。")
    device_id = request.headers.get("x-voice-terminal-device-id", "")
    core_session_id = request.headers.get("x-voice-terminal-core-session-id", "")
    challenge = request.headers.get("x-voice-terminal-challenge", "")
    timestamp_text = request.headers.get("x-voice-terminal-timestamp-ms", "")
    signature_text = request.headers.get("x-voice-terminal-signature", "")
    if (len(device_id) > 128 or len(core_session_id) > 64 or len(challenge) > 128
            or len(timestamp_text) > 20 or len(signature_text) > 128):
        raise HTTPException(status_code=401, detail="设备请求未获授权或挑战已过期。")
    try:
        timestamp_ms = int(timestamp_text, 10)
        signature = base64.b64decode(signature_text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        raise HTTPException(status_code=401, detail="设备请求未获授权或挑战已过期。")
    if not authenticator.authorize_request(
        method=method, path=request.url.path, device_id=device_id,
        core_session_id=core_session_id, challenge=challenge,
        timestamp_ms=timestamp_ms, signature=signature, payload=payload,
    ):
        raise HTTPException(status_code=401, detail="设备请求未获授权或挑战已过期。")


def _device_media_open_payload(websocket: Any) -> dict[str, Any]:
    """_device_media_open_payload（媒体握手载荷）：拒绝重复、超限和非规范查询字段。"""
    raw_query = websocket.scope.get("query_string", b"")
    if not isinstance(raw_query, bytes) or len(raw_query) > 2048:
        raise HTTPException(status_code=413, detail="设备媒体握手字段过大。")
    try:
        query_text = raw_query.decode("ascii")
        pairs = parse_qsl(query_text, keep_blank_values=True, strict_parsing=True, max_num_fields=9)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="设备媒体握手查询格式无效。") from exc
    required = {
        "protocol_version", "stream_id", "connection_generation",
        "executor_generation", "interaction_generation",
    }
    codec_fields = {"uplink_codec", "downlink_codec", "encoder_pre_skip_samples"}
    downlink_pre_skip_field = "downlink_encoder_pre_skip_samples"
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise HTTPException(status_code=400, detail="设备媒体握手包含重复字段。")
        values[key] = value
    present_codec_fields = set(values) & codec_fields
    if (not required.issubset(values)
            or set(values) - required - codec_fields - {downlink_pre_skip_field}
            or present_codec_fields not in (set(), codec_fields)
            or (downlink_pre_skip_field in values and present_codec_fields != codec_fields)):
        raise HTTPException(status_code=422, detail="设备媒体握手字段不符合协议版本 2。")
    stream_id = values["stream_id"]
    if (not stream_id or len(stream_id) > 128
            or any(not (char.isascii() and (char.isalnum() or char in "._:-")) for char in stream_id)):
        raise HTTPException(status_code=422, detail="设备媒体流编号无效。")

    def positive_integer(name: str) -> int:
        value = values[name]
        if (not value or len(value) > 16 or not value.isascii() or not value.isdigit()
                or str(int(value)) != value or int(value) < 1 or int(value) > (1 << 53) - 1):
            raise HTTPException(status_code=422, detail=f"设备媒体握手字段 {name}（代次或版本）无效。")
        return int(value)

    payload = {
        "protocol_version": positive_integer("protocol_version"),
        "stream_id": stream_id,
        "connection_generation": positive_integer("connection_generation"),
        "executor_generation": positive_integer("executor_generation"),
        "interaction_generation": positive_integer("interaction_generation"),
    }
    if present_codec_fields:
        uplink_codec = values["uplink_codec"]
        downlink_codec = values["downlink_codec"]
        if uplink_codec not in {"pcm", "opus"} or downlink_codec not in {"pcm", "opus"}:
            raise HTTPException(status_code=422, detail="设备媒体握手编解码模式无效。")
        pre_skip_text = values["encoder_pre_skip_samples"]
        if (not pre_skip_text or len(pre_skip_text) > 6 or not pre_skip_text.isascii()
                or not pre_skip_text.isdigit() or str(int(pre_skip_text)) != pre_skip_text):
            raise HTTPException(status_code=422, detail="Opus 编码预延迟字段无效。")
        pre_skip = int(pre_skip_text)
        if (not 0 <= pre_skip <= 320
                or (uplink_codec == "pcm" and pre_skip != 0)
                or (uplink_codec == "opus" and pre_skip == 0)):
            raise HTTPException(status_code=422, detail="上行编码模式与预延迟样本数不匹配。")
        downlink_pre_skip_text = values.get(downlink_pre_skip_field, "0")
        if (not downlink_pre_skip_text or len(downlink_pre_skip_text) > 6
                or not downlink_pre_skip_text.isascii() or not downlink_pre_skip_text.isdigit()
                or str(int(downlink_pre_skip_text)) != downlink_pre_skip_text):
            raise HTTPException(status_code=422, detail="下行 Opus 编码预延迟字段无效。")
        downlink_pre_skip = int(downlink_pre_skip_text)
        if (not 0 <= downlink_pre_skip <= 960
                or (downlink_codec == "pcm" and downlink_pre_skip != 0)
                or (downlink_codec == "opus" and downlink_pre_skip == 0)):
            raise HTTPException(status_code=422, detail="下行编码模式与预延迟样本数不匹配。")
        payload.update({
            "uplink_codec": uplink_codec,
            "downlink_codec": downlink_codec,
            "encoder_pre_skip_samples": pre_skip,
        })
        if downlink_pre_skip_field in values:
            payload[downlink_pre_skip_field] = downlink_pre_skip
    if payload["protocol_version"] != MEDIA_PROTOCOL_VERSION:
        raise HTTPException(status_code=422, detail="设备媒体协议版本不受支持。")
    return payload


def _device_media_clock_response(message: Any, *, sync_id: str) -> tuple[int, int]:
    """_device_media_clock_response（时钟响应）：解析唯一字段并检查设备单调时钟。"""
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("设备时钟同步响应必须是 JSON 控制消息。")
    encoded = message["text"].encode("utf-8")
    if not 0 < len(encoded) <= 1024:
        raise ValueError("设备时钟同步响应大小无效。")
    payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_fields)
    required = {"type", "protocol_version", "sync_id", "device_receive_us", "device_send_us"}
    if (not isinstance(payload, dict)
            or not required.issubset(payload) or set(payload) - required - {"runtime_controls"}
            or ("runtime_controls" in payload and type(payload["runtime_controls"]) is not bool)
            or payload.get("type") != "clock.sync.response"
            or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != MEDIA_PROTOCOL_VERSION
            or payload.get("sync_id") != sync_id
            or type(payload.get("device_receive_us")) is not int
            or type(payload.get("device_send_us")) is not int
            or payload["device_receive_us"] < 0 or payload["device_send_us"] < 0):
        raise ValueError("设备时钟同步响应字段无效。")
    return payload["device_receive_us"], payload["device_send_us"]


def _device_media_playback_acceptance(
    message: Any, *, lease: Any, core_session_id: str, stream_id: str,
) -> tuple[int, bool]:
    """_device_media_playback_acceptance（设备帧回执）：区分已入队与因原键停止而丢弃。"""
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("设备播放接纳回执必须是 JSON 控制消息。")
    encoded = message["text"].encode("utf-8")
    if not 0 < len(encoded) <= 1024:
        raise ValueError("设备播放接纳回执超过控制消息上限。")
    value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_fields)
    expected = {
        "type", "protocol_version", "device_id", "delivery_device_id", "core_session_id",
        "connection_generation", "executor_generation", "interaction_generation", "stream_id",
        "sequence", "fields",
    }
    if (not isinstance(value, dict) or set(value) != expected
            or value.get("type") not in {"playback.accepted", "playback.cancelled"}
            or type(value.get("protocol_version")) is not int or value["protocol_version"] != MEDIA_PROTOCOL_VERSION
            or value.get("device_id") != lease.device_id
            or value.get("delivery_device_id") != lease.device_id
            or value.get("core_session_id") != core_session_id
            or type(value.get("connection_generation")) is not int
            or value["connection_generation"] != lease.connection_generation
            or type(value.get("executor_generation")) is not int
            or value["executor_generation"] != lease.executor_generation
            or type(value.get("interaction_generation")) is not int
            or value["interaction_generation"] != lease.interaction_generation
            or value.get("stream_id") != stream_id
            or type(value.get("sequence")) is not int or value["sequence"] < 0
            or value["sequence"] > (1 << 53) - 1
            or not isinstance(value.get("fields"), dict)):
        raise ValueError("设备播放接纳回执与固定媒体租约不匹配。")
    return value["sequence"], value["type"] == "playback.cancelled"


def _device_media_playback_progress(
    message: Any, *, lease: Any, core_session_id: str, stream_id: str,
) -> dict[str, Any]:
    """_device_media_playback_progress（设备时钟快照）：只描述实际输出观察值。"""
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        raise ValueError("设备播放快照必须是 JSON 控制消息。")
    encoded = message["text"].encode("utf-8")
    if not 0 < len(encoded) <= 16 * 1024:
        raise ValueError("设备播放快照超过控制消息上限。")
    value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json_fields)
    expected = {
        "type", "protocol_version", "device_id", "delivery_device_id", "core_session_id",
        "connection_generation", "executor_generation", "interaction_generation", "stream_id",
        "playback_key", "receipt_sequence", "sequence", "enqueued_ms", "played_ms",
        "buffered_ms", "output_estimated_ms", "started", "device_time_us", "precision", "fields",
    }
    if (not isinstance(value, dict) or set(value) != expected
            or value.get("type") != "playback.progress"
            or type(value.get("protocol_version")) is not int or value["protocol_version"] != MEDIA_PROTOCOL_VERSION
            or value.get("device_id") != lease.device_id
            or value.get("delivery_device_id") != lease.device_id
            or value.get("core_session_id") != core_session_id
            or type(value.get("connection_generation")) is not int
            or value["connection_generation"] != lease.connection_generation
            or type(value.get("executor_generation")) is not int
            or value["executor_generation"] != lease.executor_generation
            or type(value.get("interaction_generation")) is not int
            or value["interaction_generation"] != lease.interaction_generation
            or value.get("stream_id") != stream_id
            or not isinstance(value.get("playback_key"), str)
            or not isinstance(value.get("precision"), str)
            or value["precision"] not in {"callback", "device_time", "estimated", "unknown"}
            or type(value.get("started")) is not bool
            or not isinstance(value.get("fields"), dict)):
        raise ValueError("设备播放快照身份、字段或精度无效。")
    counters = (
        "receipt_sequence", "sequence", "enqueued_ms", "played_ms", "buffered_ms",
        "output_estimated_ms", "device_time_us",
    )
    for name in counters:
        item = value[name]
        if type(item) is not int or not 0 <= item <= (1 << 53) - 1:
            raise ValueError(f"设备播放快照字段 {name}（进度数值）无效。")
    if (value["receipt_sequence"] < 1 or value["played_ms"] > value["enqueued_ms"]
            or value["output_estimated_ms"] > value["enqueued_ms"] or value["buffered_ms"] > 2_000):
        raise ValueError("设备播放快照超出有效时序或队列范围。")
    return {name: value[name] for name in expected if name != "fields"}


async def _device_pairing_payload(request: Any) -> dict[str, Any]:
    """_device_pairing_payload（有界配对申请）：限制正文、字段、版本及重复键。"""
    try:
        declared_length = request.headers.get("content-length")
        if declared_length is not None and int(declared_length) > 4096:
            raise HTTPException(status_code=413, detail="配对申请超过大小上限。")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                raise HTTPException(status_code=413, detail="配对申请超过大小上限。")
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks), object_pairs_hook=_unique_json_fields)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="配对申请不是有效且唯一字段的 JSON。") from exc
    expected = {"protocol_version", "device_id", "public_key", "pairing_code", "client_nonce", "signature"}
    if (not isinstance(payload, dict) or set(payload) != expected
            or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
            or not isinstance(payload.get("device_id"), str)
            or not isinstance(payload.get("public_key"), str)
            or not isinstance(payload.get("pairing_code"), str)
            or not isinstance(payload.get("client_nonce"), str)
            or not isinstance(payload.get("signature"), str)):
        raise HTTPException(status_code=422, detail="配对申请字段或协议版本无效。")
    try:
        nonce = base64.b64decode(payload["client_nonce"].encode("ascii"), validate=True)
        signature = base64.b64decode(payload["signature"].encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="配对签名材料编码无效。") from exc
    return {**payload, "client_nonce_bytes": nonce, "signature_bytes": signature}


def _valid_command(message: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(message, dict) or message.get("type") != "command":
        return None
    request_id = message.get("request_id")
    command = message.get("command")
    payload = message.get("payload")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    if not isinstance(command, str) or not isinstance(payload, dict):
        return None
    return command, payload


def _speaker_relaxation(config: Any) -> float:
    """continuity_relaxation（连续主人放宽量）：保留草稿值，仅旧配置由绝对阈值推导。"""
    value = config.continuity_relaxation
    return float(value) if value is not None else min(0.20, max(0.0, config.confirm_threshold - config.continuity_threshold))


def _minimax_voice_ids(payload: Any) -> set[str]:
    """Collect voice_id（音色标识）values from every category in an API response."""
    found: set[str] = set()
    if isinstance(payload, Mapping):
        voice_id = payload.get("voice_id")
        if isinstance(voice_id, str) and voice_id.strip():
            found.add(voice_id.strip())
        for value in payload.values():
            found.update(_minimax_voice_ids(value))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found.update(_minimax_voice_ids(value))
    return found


def _minimax_voice_name(payload: Any, voice_id: str) -> tuple[bool, str | None]:
    """Find voice_name（音色名称）for one voice_id（音色标识），without a fixed catalog."""
    if isinstance(payload, Mapping):
        if isinstance(payload.get("voice_id"), str) and payload["voice_id"].strip() == voice_id:
            name = payload.get("voice_name")
            return True, name.strip() if isinstance(name, str) and name.strip() else None
        for value in payload.values():
            found, name = _minimax_voice_name(value, voice_id)
            if found:
                return found, name
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found, name = _minimax_voice_name(value, voice_id)
            if found:
                return found, name
    return False, None


async def _minimax_voice_catalog(runtime: Any) -> Mapping[str, Any]:
    """Fetch MiniMax's live voice catalog, keeping api_key（接口密钥）server-side."""
    tts_config = runtime.config.tts
    api_key = runtime._api_key(tts_config.api_key_env)
    if not api_key:
        raise HTTPException(status_code=422, detail=f"未配置 {tts_config.api_key_env}，无法查询 MiniMax 音色。")
    base_url = str(tts_config.base_url).rstrip("/")
    endpoint = f"{base_url}/get_voice" if base_url.endswith("/v1") else f"{base_url}/v1/get_voice"
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"voice_type": "all"},
            )
    except Exception as exc:
        logger.warning("minimax.voice_lookup_failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="无法连接 MiniMax 音色查询接口，请检查网络后重试。") from exc
    if response.status_code >= 400:
        logger.warning("minimax.voice_lookup_failed status=%d", response.status_code)
        raise HTTPException(status_code=502, detail=f"MiniMax 音色查询接口返回 HTTP {response.status_code}。")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MiniMax 音色查询接口没有返回有效 JSON。") from exc
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=502, detail="MiniMax 音色查询接口返回格式无效。")
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, Mapping):
        status_code = base_resp.get("status_code")
        if status_code not in (None, 0, "0"):
            raise HTTPException(status_code=502, detail="MiniMax 音色查询接口返回业务错误。")
    return payload


async def _validate_minimax_voice_id(runtime: Any, voice_id: str) -> None:
    """Validate arbitrary official MiniMax voice_id（音色标识），不固定音色目录。"""
    payload = await _minimax_voice_catalog(runtime)
    if voice_id not in _minimax_voice_ids(payload):
        raise HTTPException(status_code=422, detail="该音色 ID 未出现在 MiniMax 官方音色查询结果中。")


def create_app(runtime: SessionRuntime, *, lifespan: Any = None,
               model_registry: ModelRegistryService | None = None,
               product_settings: ProductSettingsStore | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi，请先运行 scripts\\setup.ps1。")

    app = FastAPI(title="Voice Terminal Server", version=__version__, lifespan=lifespan)
    aniflive_import_service = None

    def aniflive_import_or_503():
        nonlocal aniflive_import_service
        if aniflive_import_service is not None:
            return aniflive_import_service
        local_root = getattr(runtime, "_runtime_local_root", None)
        paths = getattr(runtime, "paths", None)
        install_root = getattr(paths, "install_root", None)
        data_root = (getattr(paths, "data_root", None) or
                     (Path(install_root) / "data" if install_root is not None else None))
        if local_root is None or data_root is None:
            raise HTTPException(status_code=503, detail="AnifLive 模型导入暂存目录尚未就绪。")
        try:
            from ..tts.aniflive_import import AnifLiveImportService
            aniflive_import_service = AnifLiveImportService(local_root, data_root, voice_libraries=getattr(runtime, 'voice_library', None))
            return aniflive_import_service
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"AnifLive 模型导入暂存目录不可用：{exc}") from exc

    # 恢复尚在运行或等待参考登记的导入交接，不依赖用户重新打开设置页。
    if getattr(runtime, 'voice_library', None) is not None and getattr(runtime, '_runtime_local_root', None) is not None:
        try:
            aniflive_import_or_503().pending()
        except HTTPException:
            pass  # 现有接口会在用户访问时返回具体错误，不阻断无关功能。
    configured_origins = [
        item.strip() for item in os.environ.get("VOICE_WEB_ORIGINS", "").split(",")
        if item.strip() and item.strip() != "*"
    ]
    # VOICE_WEB_ORIGINS（界面允许来源）由宿主传入当前桌面实际地址，禁止浏览器默认回退。
    origins = list(dict.fromkeys(configured_origins))
    try:
        from fastapi.middleware.cors import CORSMiddleware  # type: ignore
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "PUT", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
        )
    except ImportError:  # pragma: no cover - FastAPI installs middleware with the app
        pass

    # 18765（主机回环网关端口）复用此 ASGI（异步服务接口），但仅通过直连套接字提供显式允许的状态入口。
    app.add_middleware(
        LoopbackDeviceGatewayGuard,
        gateway_port=18_765,
        allowed_paths=frozenset({"/v1/challenge", "/v1/status", "/v1/devices", "/v1/pairing/request",
                                 "/v1/session/open", "/v1/session/poll", "/v1/session/complete",
                                 "/v1/session/reconcile", "/v1/session/heartbeat", "/v1/session/close",
                                 "/v1/interaction/prepare", "/v1/interaction/abort", "/v1/management",
                                 "/v1/file-transfer", "/v1/media", "/ws"}),
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health_payload()

    @app.post("/v1/challenge")
    async def device_gateway_challenge(request: Request) -> JSONResponse:
        """device_gateway_challenge（一次性签名挑战）：只授权固定的只读网关路径。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_challenge_payload(request)
        authenticator = getattr(runtime, "device_request_authenticator", None)
        trust_store = getattr(runtime, "device_trust_store", None)
        if authenticator is None or trust_store is None or trust_store.state != "valid":
            raise HTTPException(status_code=503, detail="主机设备信任服务不可用。")
        try:
            challenge = authenticator.issue_challenge(
                payload["device_id"], payload["method"], payload["path"],
            )
        except DeviceTrustError as exc:
            raise HTTPException(status_code=422, detail="设备签名挑战字段无效。") from exc
        return JSONResponse(challenge, headers={"Cache-Control": "no-store"})

    @app.post("/v1/file-transfer")
    async def device_gateway_file_transfer(request: Request) -> JSONResponse:
        """device_gateway_file_transfer（签名 FILE 文件操作）：只操作既有 DSH 文件票据。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备文件通道操作", max_bytes=128 * 1024)
        action = payload.get("action")
        if type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1:
            raise HTTPException(status_code=422, detail="设备文件通道协议版本无效。")
        store = getattr(runtime, "device_file_transfer_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")

        if action == "begin":
            base = {"protocol_version", "action", "transfer_id", "operation", "parent_request_id",
                    "connection_generation", "executor_generation", "maximum_bytes", "fields"}
            expected = base | ({"expected_sha256"} if payload.get("operation") == "file.sftp.download" else set())
            if (set(payload) != expected or not isinstance(payload.get("operation"), str)
                    or payload["operation"] not in {"file.sftp.upload", "file.sftp.download"}
                    or not isinstance(payload.get("transfer_id"), str)
                    or len(payload["transfer_id"]) != 32
                    or any(character not in "0123456789abcdef" for character in payload["transfer_id"])
                    or not valid_device_request_id(payload.get("parent_request_id"))
                    or type(payload.get("connection_generation")) is not int
                    or payload["connection_generation"] < 1
                    or type(payload.get("executor_generation")) is not int
                    or payload["executor_generation"] < 1
                    or type(payload.get("maximum_bytes")) is not int
                    or not 1 <= payload["maximum_bytes"] <= 4 * 1024 * 1024 * 1024
                    or not isinstance(payload.get("fields"), dict)
                    or set(payload["fields"]) != expected - {"fields"}
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 120
                           for value in payload["fields"].values())):
                raise HTTPException(status_code=422, detail="设备文件通道开始请求字段无效。")
            _authorize_device_gateway_request(runtime, request, payload)
            device_id = request.headers.get("x-voice-terminal-device-id", "")
            connection = runtime.device_registry.connection(device_id)
            if (device_id == "local" or connection is None or not connection.connected
                    or "file_transfer" not in connection.capabilities
                    or payload["connection_generation"] != connection.connection_generation
                    or payload["executor_generation"] != connection.executor_generation
                    or payload["transfer_id"] != device_file_transfer_id(connection, payload["parent_request_id"])):
                raise HTTPException(status_code=409, detail="文件传输票据与已认证设备、原请求或连接代次不匹配。")
            expected_sha256 = payload.get("expected_sha256")
            try:
                transfer = await asyncio.to_thread(store.open_device_file_channel,
                    transfer_id=payload["transfer_id"], device_id=device_id,
                    connection_generation=connection.connection_generation,
                    executor_generation=connection.executor_generation,
                    operation=payload["operation"], maximum_bytes=payload["maximum_bytes"],
                    expected_sha256=expected_sha256)
            except Exception as exc:
                raise HTTPException(status_code=409, detail="设备文件通道未匹配到可用的原 DSH 审批票据。") from exc
            return JSONResponse({"protocol_version": 1, "result": "accepted", "transfer": transfer,
                "fields": {"protocol_version": "设备文件通道协议版本", "result": "固定文件通道操作结果",
                           "transfer": "原 DSH 票据签发的短期文件通道能力与长度摘要"}},
                headers={"Cache-Control": "no-store"})

        action_fields = {
            "upload_chunk": {"offset", "data_base64", "chunk_sha256"},
            "read_chunk": {"offset", "limit"},
            "finish_upload": {"expected_bytes", "expected_sha256"},
            "finish_download": {"expected_bytes", "expected_sha256"},
            "cancel": set(),
        }
        if not isinstance(action, str) or action not in action_fields:
            raise HTTPException(status_code=422, detail="设备文件通道操作不在固定白名单中。")
        base = {"protocol_version", "action", "device_id", "connection_generation",
                "executor_generation", "transfer_id", "channel_token", "fields"}
        expected = base | action_fields[action]
        if (set(payload) != expected or not valid_device_request_id(payload.get("device_id"))
                or payload["device_id"] == "local"
                or type(payload.get("connection_generation")) is not int
                or type(payload.get("executor_generation")) is not int
                or not isinstance(payload.get("transfer_id"), str)
                or len(payload["transfer_id"]) != 32
                or any(character not in "0123456789abcdef" for character in payload["transfer_id"])
                or not isinstance(payload.get("channel_token"), str)
                or not 32 <= len(payload["channel_token"]) <= 128
                or not isinstance(payload.get("fields"), dict)
                or set(payload["fields"]) != expected - {"fields"}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 120
                       for value in payload["fields"].values())):
            raise HTTPException(status_code=422, detail="设备文件通道续传请求字段无效。")
        connection = runtime.device_registry.connection(payload["device_id"])
        if (connection is None or not connection.connected or "file_transfer" not in connection.capabilities
                or payload["connection_generation"] != connection.connection_generation
                or payload["executor_generation"] != connection.executor_generation):
            raise HTTPException(status_code=409, detail="设备文件通道对应的受信设备或执行器代次已变化。")
        key = {"transfer_id": payload["transfer_id"], "device_id": payload["device_id"],
               "connection_generation": connection.connection_generation,
               "executor_generation": connection.executor_generation,
               "channel_token": payload["channel_token"]}
        try:
            if action == "upload_chunk":
                if (type(payload["offset"]) is not int or not isinstance(payload["data_base64"], str)
                        or not isinstance(payload["chunk_sha256"], str)):
                    raise ValueError("文件分块字段无效。")
                transfer = await asyncio.to_thread(store.append_device_channel_chunk, **key,
                    offset=payload["offset"], data_base64=payload["data_base64"],
                    chunk_sha256=payload["chunk_sha256"])
            elif action == "read_chunk":
                if type(payload["offset"]) is not int or type(payload["limit"]) is not int:
                    raise ValueError("文件读取偏移或分块上限无效。")
                transfer = await asyncio.to_thread(store.read_device_channel_chunk, **key,
                    offset=payload["offset"], limit=payload["limit"])
            elif action == "finish_upload":
                if type(payload["expected_bytes"]) is not int or not isinstance(payload["expected_sha256"], str):
                    raise ValueError("上传完成摘要字段无效。")
                transfer = await asyncio.to_thread(store.finish_device_channel_upload, **key,
                    expected_bytes=payload["expected_bytes"], expected_sha256=payload["expected_sha256"])
            elif action == "finish_download":
                if type(payload["expected_bytes"]) is not int or not isinstance(payload["expected_sha256"], str):
                    raise ValueError("下载完成摘要字段无效。")
                transfer = await asyncio.to_thread(store.finish_device_channel_download, **key,
                    expected_bytes=payload["expected_bytes"], expected_sha256=payload["expected_sha256"])
            else:
                transfer = {"cancelled": await asyncio.to_thread(store.cancel_device_file_channel, **key)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="固定文件通道能力、偏移或内容核验失败。") from exc
        return JSONResponse({"protocol_version": 1, "result": "accepted", "transfer": transfer,
            "fields": {"protocol_version": "设备文件通道协议版本", "result": "固定文件通道操作结果",
                       "transfer": "与原 DSH 票据和受信连接代次绑定的分块收据"}},
            headers={"Cache-Control": "no-store"})

    @app.post("/v1/status")
    async def device_gateway_status(request: Request) -> JSONResponse:
        """device_gateway_status（设备网关状态）：不返回会话、模型或用户数据。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_payload(request)
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        return JSONResponse(
            {
                "protocol_version": 1,
                "ready": True,
                "core_session_id": runtime.core_session_id,
                "fields": {
                    "protocol_version": "设备网关协议版本",
                    "ready": "核心 HTTP（超文本传输协议）网关是否已就绪",
                    "core_session_id": "本次权威核心运行编号",
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/devices")
    async def device_gateway_devices(request: Request) -> JSONResponse:
        """device_gateway_devices（设备网关快照）：只返回设备编号、能力和租约状态。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_payload(request)
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        return JSONResponse(
            {"protocol_version": 1, "ready": True, **device_snapshot()},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/management")
    async def device_gateway_management(request: Request) -> JSONResponse:
        """device_gateway_management（受限业务操作）：操作枚举映射到现有 HTTP 业务处理器。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="远程业务操作", max_bytes=1_048_576)
        expected = {
            "protocol_version", "request_id", "target_host_id", "session_generation",
            "operation", "arguments", "fields",
        }
        if (set(payload) != expected or type(payload.get("protocol_version")) is not int
                or payload["protocol_version"] != 1
                or not isinstance(payload.get("request_id"), str)
                or len(payload["request_id"]) != 32
                or any(character not in "0123456789abcdef" for character in payload["request_id"])
                or not isinstance(payload.get("target_host_id"), str)
                or not payload["target_host_id"] or len(payload["target_host_id"]) > 128
                or not isinstance(payload.get("operation"), str)
                or not isinstance(payload.get("arguments"), dict)
                or not isinstance(payload.get("fields"), dict)
                or set(payload["fields"]) != expected - {"fields"}):
            raise HTTPException(status_code=422, detail="远程业务操作字段或版本无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if payload["target_host_id"] != getattr(runtime, "device_host_id", None):
            raise HTTPException(status_code=409, detail="请求目标服务端身份与当前工作区不匹配。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        connection = runtime.device_registry.connection(device_id)
        generation = payload.get("session_generation")
        if (type(generation) is not int or generation < 1 or connection is None
                or not connection.connected or connection.connection_generation != generation):
            raise HTTPException(status_code=409, detail="远程管理会话已失效，请重新连接并读取最新状态。")
        operation = payload["operation"]
        response_status = 200
        if operation == "voice.command":
            arguments = payload["arguments"]
            command = arguments.get("command")
            voice_commands = {"session.start", "session.stop", "session.reset", "session.clear"}
            if command == "session.reasoning_mode":
                if set(arguments) != {"command", "mode"} or arguments.get("mode") not in {"fast", "deep"}:
                    raise HTTPException(status_code=422, detail="语音推理模式操作参数无效。")
            elif command not in voice_commands or set(arguments) != {"command"}:
                raise HTTPException(status_code=422, detail="语音会话操作不在固定白名单中。")
            if command in {"session.start", "session.stop"} and not runtime.remote_media_control_ready(device_id, generation):
                raise HTTPException(status_code=409, detail="本设备媒体输入尚未就绪或已被接管，请恢复语音连接后重试。")
            if (getattr(runtime, "application_initializing", False) is True
                    and command in {"session.start", "session.reset", "session.clear"}):
                response_status = 503
                response_body = {"detail": "服务端语音核心仍在启动。"}
            else:
                try:
                    if command == "session.start":
                        await runtime.start_listening()
                    elif command == "session.stop":
                        await runtime.stop_listening()
                    elif command == "session.reset":
                        await runtime.reset_session()
                    elif command == "session.clear":
                        await runtime.clear_session()
                    else:
                        if not await runtime.set_reasoning_mode(arguments["mode"]):
                            raise ValueError("服务端没有接受该语音推理模式。")
                    runtime_state = runtime.snapshot().payload
                    result = "completed"
                    if command == "session.start" and not runtime.state.listening:
                        pending = (runtime_operations(runtime).holds
                                   or getattr(runtime, "_listen_resume_requested", False)
                                   or getattr(runtime, "_tts_resume_requested", False)
                                   or getattr(runtime, "_enhancement_resume_requested", False))
                        result = "deferred" if pending else "failed"
                        response_status = 202 if pending else 503
                    response_body = {"result": result, "runtime": runtime_state}
                    if result == "failed":
                        reason = runtime.state.can_start_reason or {}
                        response_body["detail"] = reason.get("message") or "服务端未能开始监听，请检查能力状态后重试。"
                    device_event(logger, "device.voice_command", device_id=device_id, command=command, result=result,
                                 connection_generation=generation)
                except ValueError as exc:
                    response_status = 422
                    response_body = {"detail": str(exc)[:320]}
                except Exception as exc:
                    logger.exception("remote voice command failed operation=%s", command)
                    response_status = 503
                    response_body = {"detail": "服务端没有完成该语音操作，请读取最新状态后重试。"}
        else:
            try:
                method, internal_path, internal_body = _remote_management_request_spec(
                    operation, payload["arguments"],
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if operation in {
                "tts.assets.import.prepare", "tts.assets.import.commit", "tts.assets.import.abort",
                "tts.assets.audio.read",
            }:
                if "file_transfer" not in connection.capabilities:
                    raise HTTPException(status_code=403, detail="远程设备未获准使用文件传输能力。")
                internal_body = {
                    **(internal_body or {}),
                    "_authenticated_device_id": device_id,
                    "_connection_generation": connection.connection_generation,
                    "_executor_generation": connection.executor_generation,
                }
            try:
                import httpx

                transport = httpx.ASGITransport(app=app)
                internal_headers = {"Accept": "application/json"}
                if internal_path == "/internal/application/wake-tasks":
                    # 复用内部应用路由原有宿主授权；令牌留在服务端进程，不进入远端请求与响应。
                    application_token = os.environ.get("VOICE_APP_INTERNAL_TOKEN", "")
                    if application_token:
                        internal_headers["Authorization"] = "Bearer " + application_token
                if internal_path.startswith("/internal/application/tts-assets/"):
                    # TTS 素材导入端点只接受本进程 ASGI 转发；令牌留在服务端进程。
                    application_token = os.environ.get("VOICE_APP_INTERNAL_TOKEN", "")
                    if application_token:
                        internal_headers["Authorization"] = "Bearer " + application_token
                if internal_path == "/internal/application/dsh-approval":
                    # DSH approval decisions use the same host-only authorization as the local product endpoint.
                    application_token = os.environ.get("VOICE_APP_INTERNAL_TOKEN", "")
                    if not application_token:
                        raise HTTPException(status_code=503, detail="本次应用宿主权限转发凭据不可用。")
                    internal_headers["Authorization"] = "Bearer " + application_token
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18764") as client:
                    response = await client.request(
                        method,
                        internal_path,
                        json=internal_body,
                        headers=internal_headers,
                    )
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("远程业务处理器暂不可用：%s", type(exc).__name__)
                raise HTTPException(status_code=503, detail="服务端现有业务处理器暂不可用。") from exc
            if len(response.content) > 2_097_152:
                raise HTTPException(status_code=502, detail="服务端业务回执超过大小上限。")
            response_status = response.status_code
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"detail": "服务端业务回执不是 JSON。"}
        return JSONResponse({
            "protocol_version": 1,
            "request_id": payload["request_id"],
            "target_host_id": payload["target_host_id"],
            "session_generation": generation,
            "operation": operation,
            "status": response_status,
            "body": response_body,
            "fields": {
                "protocol_version": "远程管理协议版本",
                "request_id": "服务端回传的原操作编号",
                "target_host_id": "本次操作所属服务端设备编号",
                "session_generation": "本次已认证管理会话代次",
                "operation": "服务端固定业务操作枚举",
                "status": "既有服务端业务处理器的原 HTTP 状态码",
                "body": "既有业务逻辑返回的结果或原始校验错误",
            },
        }, headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/open")
    async def device_gateway_session_open(request: Request) -> JSONResponse:
        """device_gateway_session_open（注册远端任务通道）：不授予语音交互租约。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备会话注册")
        if (set(payload) != {"protocol_version", "capabilities"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or not isinstance(payload.get("capabilities"), list)):
            raise HTTPException(status_code=422, detail="远端设备会话注册字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            connection = await runtime.open_remote_device_session(device_id, payload["capabilities"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="远端设备能力或编号无效。") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="远端设备任务通道暂不可用。") from exc
        return JSONResponse({
            "protocol_version": 1,
            "result": "accepted",
            "device_id": connection.device_id,
            "core_session_id": runtime.core_session_id,
            "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation,
            "capabilities": list(connection.capabilities),
            "fields": {
                "protocol_version": "设备任务通道协议版本",
                "result": "会话注册结果",
                "device_id": "固定远端设备编号",
                "core_session_id": "当前权威核心运行编号",
                "connection_generation": "主机授予的连接代次",
                "executor_generation": "主机授予的设备执行器代次",
                "capabilities": "本次已登记设备能力编号",
            },
        }, headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/poll")
    async def device_gateway_session_poll(request: Request) -> JSONResponse:
        """device_gateway_session_poll（客户端拉取任务）：仅返回该设备的原目标任务。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备任务轮询")
        if (set(payload) != {"protocol_version", "connection_generation", "executor_generation", "wait_ms"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or type(payload.get("connection_generation")) is not int or payload["connection_generation"] < 1
                or type(payload.get("executor_generation")) is not int or payload["executor_generation"] < 1
                or type(payload.get("wait_ms")) is not int or not 0 <= payload["wait_ms"] <= 5000):
            raise HTTPException(status_code=422, detail="设备任务轮询字段或等待时限无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            task = await runtime.poll_remote_device_task(
                device_id, payload["connection_generation"], payload["executor_generation"], payload["wait_ms"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="设备任务轮询参数无效。") from exc
        except Exception as exc:
            from ..devices.router import DeviceUnavailable
            if isinstance(exc, DeviceUnavailable):
                raise HTTPException(status_code=409, detail="远端设备会话已断开或代次已变化。") from exc
            raise HTTPException(status_code=503, detail="远端设备任务通道暂不可用。") from exc
        return JSONResponse({
            "protocol_version": 1,
            "result": "accepted",
            "device_id": device_id,
            "core_session_id": runtime.core_session_id,
            "connection_generation": payload["connection_generation"],
            "executor_generation": payload["executor_generation"],
            "task": task.payload() if task is not None else None,
            "fields": {
                "protocol_version": "设备任务通道协议版本",
                "result": "轮询结果",
                "device_id": "固定轮询设备编号",
                "core_session_id": "当前权威核心运行编号",
                "connection_generation": "当前设备连接代次",
                "executor_generation": "当前设备执行器代次",
                "task": "原目标任务；为空时本轮没有待执行任务",
            },
        }, headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/complete")
    async def device_gateway_session_complete(request: Request) -> JSONResponse:
        """device_gateway_session_complete（提交任务回执）：重复回执只确认原任务。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备任务回执", max_bytes=256 * 1024)
        expected = {"protocol_version", "connection_generation", "executor_generation", "task_id",
                    "sequence", "core_session_id", "succeeded", "result"}
        if (set(payload) != expected or type(payload.get("protocol_version")) is not int
                or payload["protocol_version"] != 1
                or type(payload.get("connection_generation")) is not int or payload["connection_generation"] < 1
                or type(payload.get("executor_generation")) is not int or payload["executor_generation"] < 1
                or not valid_device_request_id(payload.get("task_id"))
                or type(payload.get("sequence")) is not int or payload["sequence"] < 1
                or not isinstance(payload.get("core_session_id"), str)
                or type(payload.get("succeeded")) is not bool or "result" not in payload):
            raise HTTPException(status_code=422, detail="设备任务回执字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            receipt = await runtime.complete_remote_device_task(
                device_id=device_id,
                connection_generation=payload["connection_generation"],
                executor_generation=payload["executor_generation"],
                task_id=payload["task_id"], sequence=payload["sequence"],
                core_session_id=payload["core_session_id"], succeeded=payload["succeeded"],
                result=payload["result"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="设备任务回执内容无效。") from exc
        except Exception as exc:
            from ..devices.router import DeviceUnavailable
            if isinstance(exc, DeviceUnavailable):
                raise HTTPException(status_code=409, detail="设备任务回执与原任务或代次不匹配。") from exc
            raise HTTPException(status_code=503, detail="设备任务回执暂不可结算。") from exc
        return JSONResponse({"protocol_version": 1, "result": receipt, "task_id": payload["task_id"],
                             "fields": {"protocol_version": "设备任务通道协议版本",
                                        "result": "原任务回执结算结果",
                                        "task_id": "原持久设备任务编号"}},
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/reconcile")
    async def device_gateway_session_reconcile(request: Request) -> JSONResponse:
        """device_gateway_session_reconcile（跨连接回执恢复）：只结算原未知任务，不派发副作用。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="跨连接原任务回执恢复", max_bytes=256 * 1024)
        expected = {"protocol_version", "task_id", "core_session_id", "connection_generation",
                    "executor_generation", "sequence", "succeeded", "result"}
        if (set(payload) != expected or type(payload.get("protocol_version")) is not int
                or payload["protocol_version"] != 1
                or not valid_device_request_id(payload.get("task_id"))
                or not isinstance(payload.get("core_session_id"), str)
                or not payload["core_session_id"] or len(payload["core_session_id"]) > 128
                or payload["core_session_id"] != payload["core_session_id"].strip()
                or any(type(payload.get(name)) is not int or payload[name] < 1 for name in
                       ("connection_generation", "executor_generation", "sequence"))
                or type(payload.get("succeeded")) is not bool or "result" not in payload):
            raise HTTPException(status_code=422, detail="跨连接原任务回执字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            result = await runtime.reconcile_remote_device_task_receipt(
                device_id=device_id,
                task_id=payload["task_id"],
                core_session_id=payload["core_session_id"],
                connection_generation=payload["connection_generation"],
                executor_generation=payload["executor_generation"],
                sequence=payload["sequence"],
                succeeded=payload["succeeded"],
                result=payload["result"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="跨连接原任务回执内容无效。") from exc
        except Exception as exc:
            from ..devices.router import DeviceUnavailable
            if isinstance(exc, DeviceUnavailable):
                raise HTTPException(status_code=409, detail="跨连接回执与原任务记录或设备代次不匹配。") from exc
            raise HTTPException(status_code=503, detail="跨连接原任务回执暂不可结算。") from exc
        return JSONResponse({"protocol_version":1,"task_id":payload["task_id"],"result":result,
            "fields":{"protocol_version":"设备任务通道协议版本",
                      "task_id":"原持久设备任务编号",
                      "result":"原任务回执恢复结果；pending/unknown 时客户端保留本地回执"}},
            headers={"Cache-Control":"no-store"})

    @app.post("/v1/session/heartbeat")
    async def device_gateway_session_heartbeat(request: Request) -> JSONResponse:
        """device_gateway_session_heartbeat（长任务续租）：不领取任务、不变更设备或执行器代次。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备长任务续租")
        if (set(payload) != {"protocol_version", "connection_generation", "executor_generation"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or type(payload.get("connection_generation")) is not int or payload["connection_generation"] < 1
                or type(payload.get("executor_generation")) is not int or payload["executor_generation"] < 1):
            raise HTTPException(status_code=422, detail="设备长任务续租字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            result = await runtime.heartbeat_remote_device_session(
                device_id, payload["connection_generation"], payload["executor_generation"],
            )
        except Exception as exc:
            from ..devices.router import DeviceUnavailable
            if isinstance(exc, DeviceUnavailable):
                raise HTTPException(status_code=409, detail="设备长任务续租对应会话已断开或代次已变化。") from exc
            raise HTTPException(status_code=503, detail="设备长任务续租暂不可用。") from exc
        return JSONResponse({"protocol_version": 1, "result": result, "device_id": device_id,
            "connection_generation": payload["connection_generation"],
            "executor_generation": payload["executor_generation"],
            "fields": {"protocol_version": "设备任务通道协议版本", "result": "连接租约续租结果",
                       "device_id": "固定设备编号", "connection_generation": "当前连接代次",
                       "executor_generation": "当前执行器代次"}}, headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/close")
    async def device_gateway_session_close(request: Request) -> JSONResponse:
        """device_gateway_session_close（关闭设备任务通道）：不重放或迁移在途任务。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="设备会话关闭")
        if (set(payload) != {"protocol_version", "connection_generation", "executor_generation"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or type(payload.get("connection_generation")) is not int or payload["connection_generation"] < 1
                or type(payload.get("executor_generation")) is not int or payload["executor_generation"] < 1):
            raise HTTPException(status_code=422, detail="设备会话关闭字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        closed = await runtime.close_remote_device_session(device_id, payload["connection_generation"])
        return JSONResponse({"protocol_version": 1, "result": "closed" if closed else "already_closed",
                             "device_id": device_id,
                             "fields": {"protocol_version": "设备任务通道协议版本",
                                        "result": "关闭结果",
                                        "device_id": "固定远端设备编号"}},
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/interaction/prepare")
    async def device_gateway_interaction_prepare(request: Request) -> JSONResponse:
        """device_gateway_interaction_prepare（准备语音设备切换）：切换由用户显式请求。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="语音设备切换请求")
        if (set(payload) != {"protocol_version", "expected_interaction_generation"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or type(payload.get("expected_interaction_generation")) is not int
                or payload["expected_interaction_generation"] < 0):
            raise HTTPException(status_code=422, detail="语音设备切换请求字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        if getattr(runtime, "application_initializing", False) is True:
            raise HTTPException(status_code=503, detail="语音核心仍在启动。")
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        try:
            lease = await runtime.prepare_interaction_switch(
                device_id, payload["expected_interaction_generation"],
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="语音设备状态已变化或切换目标无效。") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail="语音设备未就绪或另一个切换事务正在进行。") from exc
        return JSONResponse({
            "protocol_version": 1,
            "result": "active" if runtime.device_registry.accepts(lease) else "pending_media",
            "device_id": lease.device_id,
            "core_session_id": runtime.core_session_id,
            "connection_generation": lease.connection_generation,
            "executor_generation": lease.executor_generation,
            "interaction_generation": lease.interaction_generation,
            "fields": {
                "protocol_version": "语音切换协议版本",
                "result": "已经活动或等待媒体设备就绪",
                "device_id": "用户明确选择的语音设备编号",
                "core_session_id": "权威核心运行编号",
                "connection_generation": "目标设备连接代次",
                "executor_generation": "目标设备执行器代次",
                "interaction_generation": "主机准备授予的新语音占用代次",
            },
        }, headers={"Cache-Control": "no-store"})

    @app.post("/v1/interaction/abort")
    async def device_gateway_interaction_abort(request: Request) -> JSONResponse:
        """device_gateway_interaction_abort（放弃未完成切换）：不恢复旧占用权。"""
        _require_device_gateway_port(request)
        payload = await _device_gateway_json(request, label="语音设备切换撤销")
        if (set(payload) != {"protocol_version", "interaction_generation"}
                or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1
                or type(payload.get("interaction_generation")) is not int
                or payload["interaction_generation"] < 1):
            raise HTTPException(status_code=422, detail="语音设备切换撤销字段无效。")
        _authorize_device_gateway_request(runtime, request, payload)
        device_id = request.headers.get("x-voice-terminal-device-id", "")
        lease = runtime.device_registry.pending
        if (lease is None or lease.device_id != device_id
                or lease.interaction_generation != payload["interaction_generation"]):
            return JSONResponse({"protocol_version": 1, "result": "already_finished",
                                 "interaction_generation": payload["interaction_generation"],
                                 "fields": {"result": "该设备代次已经完成或失效",
                                            "interaction_generation": "原待切换语音占用代次"}},
                                headers={"Cache-Control": "no-store"})
        aborted = await runtime.abort_interaction_switch(lease)
        return JSONResponse({"protocol_version": 1,
                             "result": "aborted" if aborted else "already_finished",
                             "interaction_generation": payload["interaction_generation"],
                             "fields": {"result": "切换已放弃；旧设备不会自动恢复",
                                        "interaction_generation": "原待切换语音占用代次"}},
                            headers={"Cache-Control": "no-store"})

    @app.websocket("/v1/media")
    async def device_gateway_media(websocket: WebSocket) -> None:
        """device_gateway_media（SSH 实时音频通道）：以一个认证会话承载上行及固定目标下行 PCM。"""
        capture: RemoteAudioCapture | None = None
        playback: RemotePlaybackOutbox | None = None
        client_tts: RemoteClientTtsOutbox | None = None
        lease = None
        device_id = ""
        try:
            _require_device_gateway_port(websocket)
            payload = _device_media_open_payload(websocket)
            _authorize_device_gateway_request(runtime, websocket, payload, method="GET")
            device_id = websocket.headers.get("x-voice-terminal-device-id", "")
            core_session_id = websocket.headers.get("x-voice-terminal-core-session-id", "")
            if (getattr(runtime, "application_initializing", False) is True
                    or core_session_id != runtime.core_session_id):
                raise HTTPException(status_code=409, detail="核心服务尚未就绪或运行代次已变化。")
            connection = runtime.device_registry.connection(device_id)
            expected_lease = next((candidate for candidate in (
                runtime.device_registry.active, runtime.device_registry.pending,
            ) if candidate is not None and candidate.device_id == device_id
                and candidate.connection_generation == payload["connection_generation"]
                and candidate.executor_generation == payload["executor_generation"]
                and candidate.interaction_generation == payload["interaction_generation"]), None)
            lease = expected_lease
            if (connection is None or not connection.connected
                    or "audio_stream" not in connection.capabilities
                    or lease is None
                    or not (runtime.device_registry.accepts(lease) or runtime.device_registry.pending == lease)
                    or runtime.device_remote_sessions is None
                    or not runtime.device_remote_sessions.is_connected(
                        device_id, lease.connection_generation, lease.executor_generation,
                    )):
                raise HTTPException(status_code=409, detail="媒体连接没有当前远端语音交互租约。")
        except HTTPException as exc:
            device_event(logger, "device.media_handshake_rejected", device_id=websocket.headers.get("x-voice-terminal-device-id", ""),
                         result=str(exc.status_code), reason=str(exc.detail))
            await websocket.close(code=1008)
            return
        except Exception:
            logger.exception("device.media_handshake_failed")
            await websocket.close(code=1008)
            return

        await websocket.accept()
        opened_at = media_clock()
        close_reason = "对端关闭"  # close_reason（连接结束原因）：正常关闭也进入诊断。
        media_context = dict(device_id=device_id, core_session_id=runtime.core_session_id,
                             connection_generation=lease.connection_generation,
                             executor_generation=lease.executor_generation,
                             interaction_generation=lease.interaction_generation, stream_id=payload["stream_id"])
        device_event(logger, "device.media_opened", **media_context)
        mapper = DeviceClockMapper()
        clock_sync = MediaClockSync(mapper)
        runtime_controls_enabled = False  # runtime_controls_enabled（客户端已明确支持续测及输入重建）。

        async def request_clock(sync_id: str) -> None:
            await websocket.send_json({
                "type": "clock.sync.request", "protocol_version": MEDIA_PROTOCOL_VERSION,
                "sync_id": sync_id,
                "fields": {"type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                           "sync_id": "本次往返时钟测量编号", "runtime_controls": "支持运行期续测和输入流重建"},
            })

        def observe_clock(message: dict, received_at: float) -> None:
            nonlocal runtime_controls_enabled
            control = json.loads(message.get("text", ""), object_pairs_hook=_unique_json_fields)
            sync_id = control.get("sync_id") if isinstance(control, dict) else None
            if not isinstance(sync_id, str) or not 0 < len(sync_id) <= 128:
                raise MediaProtocolError("设备时钟响应缺少有效测量编号。")
            device_receive, device_send = _device_media_clock_response(message, sync_id=sync_id)
            runtime_controls_enabled |= control.get("runtime_controls") is True
            sent_at = clock_sync.pending.get(sync_id)
            try:
                clock_sync.observe(sync_id, device_receive_us=device_receive,
                                   device_send_us=device_send, host_receive=received_at)
            except ValueError as exc:
                device_event(logger, "device.clock_sample_rejected", level=logging.WARNING,
                             **media_context, sync_id=sync_id, host_send=sent_at,
                             host_receive=received_at, device_receive_us=device_receive,
                             device_send_us=device_send, reason=str(exc))

        audio_meter = getattr(runtime, "audio_meter", None)

        def input_stream(stream_id: str) -> PcmInputStream:
            return PcmInputStream(
                stream_id=stream_id, core_session_id=runtime.core_session_id,
                lease=lease, registry=runtime.device_registry,
                map_device_time=lambda device_time_us: mapper.map_time(
                    device_time_us, now=media_clock(),
                ),
                audio_codec=payload.get("uplink_codec", "pcm"),
                encoder_pre_skip_samples=payload.get("encoder_pre_skip_samples", 0),
            )

        capture = RemoteAudioCapture(
            input_stream(payload["stream_id"]),
            on_audio_frame=(
                (lambda pcm: audio_meter.feed("input", pcm, 16_000, 1))
                if audio_meter is not None else None
            ),
        )
        playback = RemotePlaybackOutbox(
            device_id=lease.device_id, core_session_id=runtime.core_session_id,
            connection_generation=lease.connection_generation,
            executor_generation=lease.executor_generation,
            interaction_generation=lease.interaction_generation,
            stream_id=payload["stream_id"],
            downlink_codec=payload.get("downlink_codec", "pcm"),
            downlink_encoder_pre_skip_samples=payload.get("downlink_encoder_pre_skip_samples", 0),
        )
        if "client_tts" in connection.capabilities:
            client_tts = RemoteClientTtsOutbox(
                lease=lease, core_session_id=runtime.core_session_id,
                stream_id=payload["stream_id"],
            )
        try:
            await runtime.select_remote_capture(capture, lease)
            await runtime.attach_remote_playback_outbox(lease, playback)
            if client_tts is not None:
                await runtime.attach_remote_client_tts_outbox(lease, client_tts)
            for _sample_index in range(5):
                host_send = media_clock()
                sync_id = clock_sync.request(host_send, force=True)
                await request_clock(sync_id)
                deadline = asyncio.get_running_loop().time() + clock_sync.TIMEOUT_SECONDS
                try:
                    while sync_id in clock_sync.pending:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=remaining,
                        )
                        if message.get("type") == "websocket.disconnect":
                            raise WebSocketDisconnect()
                        observe_clock(message, media_clock())
                except asyncio.TimeoutError:
                    clock_sync.pending.pop(sync_id, None)
                    device_event(logger, "device.clock_sample_timeout", level=logging.WARNING,
                                 **media_context, sync_id=sync_id)

            # 在授予交互租约前确认至少一个可信样本，不能把全失败的握手标成媒体就绪。
            mapper.estimate(now=media_clock())
            if runtime_controls_enabled:
                capture.enable_restart(input_stream)
            await runtime.complete_remote_interaction_switch(capture, lease)
            refresh_client_tts = getattr(runtime, "refresh_client_tts_service", None)
            if callable(refresh_client_tts):
                await refresh_client_tts()
            estimate = mapper.estimate(now=media_clock())
            media_ready = {
                "type": "media.ready",
                "protocol_version": MEDIA_PROTOCOL_VERSION,
                "device_id": device_id,
                "stream_id": payload["stream_id"],
                "core_session_id": runtime.core_session_id,
                "connection_generation": lease.connection_generation,
                "executor_generation": lease.executor_generation,
                "interaction_generation": lease.interaction_generation,
                "sample_rate": 16_000,
                "channels": 1,
                "bits_per_sample": 16,
                "uplink_codec": payload.get("uplink_codec", "pcm"),
                "downlink_codec": payload.get("downlink_codec", "pcm"),
                "client_tts_enabled": client_tts is not None,
                "runtime_controls": runtime_controls_enabled,
                "encoder_pre_skip_samples": payload.get("encoder_pre_skip_samples", 0),
                "clock_uncertainty_us": int(round(estimate.uncertainty_seconds * 1_000_000)),
                "capture_active": capture.running,
                "fields": {
                    "type": "控制消息类型",
                    "protocol_version": "设备媒体协议版本",
                    "device_id": "通过签名授权的固定设备编号",
                    "stream_id": "本次连续音频流编号",
                    "core_session_id": "权威核心运行编号",
                    "connection_generation": "远端设备连接代次",
                    "executor_generation": "远端设备执行器代次",
                    "interaction_generation": "核心授予的活跃语音占用代次",
                    "sample_rate": "输入采样率，每秒采样数",
                    "channels": "输入声道数",
                    "bits_per_sample": "每个采样的位数",
                    "uplink_codec": "协商后的设备上行负载编码",
                    "downlink_codec": "协商后的设备下行负载编码",
                    "client_tts_enabled": "本设备是否提供本机凭据在线合成能力",
                    "runtime_controls": "客户端是否支持时钟续测和输入重建",
                    "encoder_pre_skip_samples": "上行 Opus 编码器预延迟样本数",
                    "downlink_encoder_pre_skip_samples": "下行 Opus 编码器预延迟样本数",
                    "clock_uncertainty_us": "最近设备时钟映射误差估计，微秒",
                    "capture_active": "核心当前是否已启动该设备采集",
                },
            }
            if "downlink_encoder_pre_skip_samples" in payload:
                media_ready["downlink_encoder_pre_skip_samples"] = payload["downlink_encoder_pre_skip_samples"]
            await websocket.send_json(media_ready)
            device_event(logger, "device.media_ready", **media_context, capture_active=capture.running,
                         client_tts_enabled=client_tts is not None,
                         uncertainty_us=int(round(estimate.uncertainty_seconds * 1_000_000)))

            last_capture_state = (capture.running, capture.stream_id)
            last_heartbeat = time.monotonic()
            client_tts_task = asyncio.create_task(client_tts.next_control()) if client_tts is not None else None
            while True:
                if not runtime.device_registry.accepts(lease):
                    raise MediaProtocolError("活跃语音租约已撤销。")
                now = media_clock()
                for expired_id in clock_sync.expire(now):
                    device_event(logger, "device.clock_sample_timeout", level=logging.WARNING,
                                 **media_context, sync_id=expired_id)
                sync_id = clock_sync.request(now) if runtime_controls_enabled else None
                if sync_id is not None:
                    await request_clock(sync_id)
                capture_state = (capture.running, capture.stream_id)
                if capture_state != last_capture_state:
                    await websocket.send_json({
                        "type": "media.capture_state", "protocol_version": MEDIA_PROTOCOL_VERSION,
                        "device_id": device_id, "stream_id": payload["stream_id"],
                        "capture_active": capture.running, "capture_stream_id": capture.stream_id,
                        "fields": {"type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                                   "device_id": "固定远端设备编号", "stream_id": "媒体连接编号",
                                   "capture_active": "主机是否启动语音采集", "capture_stream_id": "本轮输入流编号"},
                    })
                    device_event(logger, "device.capture_state", **media_context,
                                 capture_active=capture.running, capture_stream_id=capture.stream_id)
                    last_capture_state = capture_state
                if time.monotonic() - last_heartbeat >= 10.0:
                    await runtime.heartbeat_remote_device_session(
                        device_id, lease.connection_generation, lease.executor_generation,
                    )
                    last_heartbeat = time.monotonic()
                receive_task = asyncio.create_task(websocket.receive())
                playback_task = asyncio.create_task(playback.next_frame())
                wait_tasks = {receive_task, playback_task}
                if client_tts_task is not None:
                    wait_tasks.add(client_tts_task)
                done, pending_tasks = await asyncio.wait(
                    wait_tasks, timeout=0.25,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for pending_task in pending_tasks:
                    if pending_task is not client_tts_task:
                        pending_task.cancel()
                cancelled_pending = [task for task in pending_tasks if task is not client_tts_task]
                if cancelled_pending:
                    await asyncio.gather(*cancelled_pending, return_exceptions=True)
                if not done:
                    continue
                if receive_task in done:
                    message = receive_task.result()
                    if message.get("type") == "websocket.disconnect":
                        break
                else:
                    message = None
                if client_tts_task is not None and client_tts_task in done:
                    control = client_tts_task.result()
                    if control is None:
                        raise MediaProtocolError("客户端合成控制队列已关闭。")
                    await websocket.send_json(control)
                    client_tts_task = asyncio.create_task(client_tts.next_control())
                if playback_task in done:
                    playback_frame = playback_task.result()
                    if playback_frame is None:
                        raise MediaProtocolError("下行播放队列已关闭。")
                    await websocket.send_bytes(encode_playback_frame(playback_frame))
                if message is None:
                    continue
                frame_bytes = message.get("bytes")
                if frame_bytes is not None:
                    if not isinstance(frame_bytes, bytes) or len(frame_bytes) > 4 + 4096 + 256 * 1024:
                        raise MediaProtocolError("设备媒体二进制消息超过长度上限。")
                    try:
                        tts_frame = decode_client_tts_audio_frame(frame_bytes)
                    except MediaProtocolError:
                        capture.accept_frame(decode_frame(frame_bytes))
                    else:
                        if client_tts is None:
                            raise MediaProtocolError("设备没有协商客户端在线合成能力。")
                        await client_tts.accept_audio(tts_frame)
                    continue
                text_message = message.get("text")
                if isinstance(text_message, str) and len(text_message.encode("utf-8")) <= 16 * 1024:
                    try:
                        control = json.loads(text_message, object_pairs_hook=_unique_json_fields)
                    except Exception as exc:
                        raise MediaProtocolError("设备媒体控制消息不是有效且唯一字段的 JSON。") from exc
                    control_type = control.get("type") if isinstance(control, dict) else None
                    if control_type == "clock.sync.response":
                        observe_clock(message, media_clock())
                        continue
                    if isinstance(control_type, str) and control_type.startswith("media.tts."):
                        control = parse_tts_control(text_message)
                        if client_tts is None:
                            raise MediaProtocolError("设备没有协商客户端在线合成能力。")
                        client_tts.accept_control(control)
                        if control_type in {
                            "media.tts.output_progress", "media.tts.completed",
                            "media.tts.cancelled", "media.tts.failed",
                        }:
                            playback_key = control.get("playback_key")
                            if not isinstance(playback_key, str):
                                raise MediaProtocolError("客户端直放输出回执缺少固定播放键。")
                            snapshot = client_tts.playback_progress(playback_key)
                            record_client_tts = getattr(runtime, "record_client_tts_output_progress", None)
                            if callable(record_client_tts):
                                host_output_time, uncertainty = mapper.map_time(
                                    int(snapshot["device_time_us"]), now=media_clock(),
                                )
                                recorded = await record_client_tts(
                                    lease, payload["stream_id"], playback_key,
                                    {**snapshot, "host_output_time": host_output_time,
                                     "clock_uncertainty_us": int(round(uncertainty * 1_000_000))},
                                )
                                if not recorded:
                                    raise MediaProtocolError("客户端直放输出证据属于旧播放代次或进度倒退。")
                        continue
                    if isinstance(control, dict) and control.get("type") in {"playback.accepted", "playback.cancelled"}:
                        sequence, cancelled = _device_media_playback_acceptance(
                            message, lease=lease, core_session_id=runtime.core_session_id,
                            stream_id=payload["stream_id"],
                        )
                        if playback is None or not playback.acknowledge(sequence, cancelled=cancelled):
                            raise MediaProtocolError("设备播放帧回执重复、迟到或越序。")
                        continue
                    if isinstance(control, dict) and control.get("type") == "playback.progress":
                        if playback is None:
                            raise MediaProtocolError("媒体通道没有绑定远端播放队列。")
                        progress = _device_media_playback_progress(
                            message, lease=lease, core_session_id=runtime.core_session_id,
                            stream_id=payload["stream_id"],
                        )
                        host_output_time, uncertainty = mapper.map_time(
                            progress["device_time_us"], now=media_clock(),
                        )
                        progress["host_output_time"] = host_output_time
                        progress["clock_uncertainty_us"] = int(round(uncertainty * 1_000_000))
                        if not await runtime.record_remote_playback_progress(
                            lease, payload["stream_id"], progress,
                        ):
                            raise MediaProtocolError("设备播放快照回执重复、迟到或采样进度倒退。")
                        continue
                    if (isinstance(control, dict)
                            and set(control) == {"type", "protocol_version", "sequence"}
                            and control.get("type") == "media.ping"
                            and type(control.get("protocol_version")) is int
                            and control["protocol_version"] == MEDIA_PROTOCOL_VERSION
                            and type(control.get("sequence")) is int
                            and control["sequence"] >= 0):
                        await websocket.send_json({
                            "type": "media.pong", "protocol_version": MEDIA_PROTOCOL_VERSION,
                            "sequence": control["sequence"],
                            "fields": {"type": "控制消息类型", "protocol_version": "设备媒体协议版本",
                                       "sequence": "与请求对应的保活序号"},
                        })
                        continue
                raise MediaProtocolError("设备媒体通道只接受符合协商编码的 PCM/Opus 二进制帧或有效保活消息。")
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            close_reason = "宿主任务取消"
            raise
        except Exception as exc:
            close_reason = str(exc)
            logger.info("device.media_stream_closed device_id=%s reason=%s", device_id, str(exc)[:160])
            try:
                await websocket.close(code=1002 if isinstance(exc, (MediaProtocolError, ValueError)) else 1011)
            except Exception:
                pass
        finally:
            device_event(logger, "device.media_closed", **media_context, reason=close_reason,
                         duration_ms=int(max(0.0, media_clock() - opened_at) * 1000))
            if "client_tts_task" in locals() and client_tts_task is not None and not client_tts_task.done():
                client_tts_task.cancel()
                await asyncio.gather(client_tts_task, return_exceptions=True)
            if capture is not None and lease is not None:
                try:
                    if client_tts is not None:
                        await runtime.detach_remote_client_tts_outbox(lease, client_tts)
                    if playback is not None:
                        await runtime.detach_remote_playback_outbox(lease, playback)
                    await runtime.clear_remote_capture(capture, lease)
                    await runtime.abort_interaction_switch(lease)
                    recover_local = getattr(runtime, "recover_local_after_remote_media_loss", None)
                    if callable(recover_local):
                        await recover_local(lease, capture=capture)
                    refresh_client_tts = getattr(runtime, "refresh_client_tts_service", None)
                    if callable(refresh_client_tts):
                        await refresh_client_tts()
                except Exception:
                    logger.exception("device.media_capture_cleanup_failed device_id=%s", device_id)

    @app.post("/v1/pairing/request")
    async def device_gateway_pairing_request(request: Request) -> JSONResponse:
        """device_gateway_pairing_request（SSH 配对申请）：只接受限时码和设备私钥签名。"""
        server = request.scope.get("server")
        if not isinstance(server, (tuple, list)) or len(server) != 2 or server[1] != 18_765:
            raise HTTPException(status_code=404, detail="配对申请只允许通过受限设备回环网关。")
        payload = await _device_pairing_payload(request)
        manager = getattr(runtime, "device_pairing_manager", None)
        trust_store = getattr(runtime, "device_trust_store", None)
        if manager is None or trust_store is None or trust_store.state == "invalid":
            raise HTTPException(status_code=503, detail="主机信任登记不可用，配对申请已拒绝。")
        try:
            pending = manager.submit_request(
                payload["device_id"], payload["public_key"], payload["pairing_code"],
                payload["client_nonce_bytes"], payload["signature_bytes"],
            )
        except (DeviceTrustError, ValueError) as exc:
            raise HTTPException(status_code=403, detail="设备配对申请未获授权或配对窗口已关闭。") from exc
        return JSONResponse(
            {
                "protocol_version": 1,
                "request_id": pending.request_id,
                "device_id": pending.device_id,
                "fingerprint": pending.fingerprint,
                "created_at_ms": pending.created_at_ms,
                "expires_at_ms": pending.expires_at_ms,
                "fields": {
                    "protocol_version": "配对协议版本",
                    "request_id": "待主机批准的申请编号",
                    "device_id": "申请设备编号",
                    "fingerprint": "申请设备公钥 SHA-256 指纹",
                    "created_at_ms": "请求建立时间，Unix 毫秒",
                    "expires_at_ms": "请求到期时间，Unix 毫秒",
                },
            },
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def startup_gate(request: Request, call_next):
        """startup_gate（启动门禁）：允许设置与查询，声纹采集等待模型加载结束。"""
        if (getattr(runtime, "application_initializing", False) is True
                and request.method not in {"GET", "OPTIONS"}
                and request.url.path.startswith("/api/v1/speaker/")):
            return JSONResponse({"detail": "组件仍在加载，请等待启动完成后采集或测试声纹。"}, status_code=503)
        return await call_next(request)

    def authorize_application(request: Request) -> None:
        token = os.environ.get("VOICE_APP_INTERNAL_TOKEN", "")
        remote = request.client.host if request.client else ""
        if not token or remote not in {"127.0.0.1", "::1"} or not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + token
        ):
            raise HTTPException(status_code=403, detail="仅允许本次应用宿主控制组件。")

    @app.get("/internal/application/status")
    async def application_status(request: Request) -> dict:
        authorize_application(request)
        return await runtime.application_delegated_status()

    @app.post("/internal/application/device-gateway")
    async def application_device_gateway(request: Request) -> dict:
        """application_device_gateway（交接回环入口）：仅接收本次受管宿主的显式开关。"""
        authorize_application(request)
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {"enabled", "host_id"} or type(payload["enabled"]) is not bool:
            raise HTTPException(status_code=422, detail="设备网关开关字段无效。")
        gateway = getattr(runtime, "_device_gateway", None)
        if gateway is None or getattr(runtime, "_application_stopping", False):
            raise HTTPException(status_code=503, detail="设备回环网关控制器不可用。")
        host_id = payload["host_id"]
        if payload["enabled"]:
            if not isinstance(host_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", host_id):
                raise HTTPException(status_code=422, detail="设备网关主机编号无效。")
            runtime.device_host_id = host_id
        return await gateway.set_enabled(payload["enabled"])

    @app.post("/internal/application/control")
    async def application_control(request: Request) -> dict:
        authorize_application(request)
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("action") not in {
            "pause", "dsh-start", "dsh-retry", "dsh-wait", "speaker-retry", "capability-retry",
            "reinitialize", "initialize-local", "initialize-session"
        }:
            raise HTTPException(status_code=422, detail="受管操作无效。")
        capability = payload.get("capability")
        if payload["action"] == "capability-retry":
            if not isinstance(capability, str) or capability not in {
                "audio", "asr", "llm", "tts", "kws", "speaker",
            }:
                raise HTTPException(status_code=422, detail="组件能力标识无效。")
        if payload["action"] in {"reinitialize", "initialize-session"}:
            fields = (("asr_available", "VOICE_APP_ASR_AVAILABLE"),
                      ("analysis_available", "VOICE_APP_ANALYSIS_AVAILABLE"),
                      ("asr_managed", "VOICE_APP_ASR_MANAGED"),
                      ("analysis_managed", "VOICE_APP_ANALYSIS_MANAGED"))
            # 全部字段验证成功后再更新环境，避免无效请求留下半份依赖状态。
            if any(type(payload.get(field)) is not bool for field, _ in fields):
                raise HTTPException(status_code=422, detail="缺少受管工作进程状态。")
            for field, environment in fields:
                os.environ[environment] = "1" if payload[field] else "0"
        endpoint_table = None
        if "endpoints" in payload:
            if payload["action"] not in {"reinitialize", "initialize-session"} or not isinstance(payload["endpoints"], dict):
                raise HTTPException(status_code=422, detail="受管端点表无效。")
            endpoint_table = {}
            for component in ("asr", "analysis", "speaker"):
                value = payload["endpoints"].get(component)
                if value is None:
                    continue
                try:
                    endpoint_table[component] = str(validate_endpoint_url(component, value)["url"])
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail="受管端点表包含无效地址。") from exc
        return await runtime.application_control(payload["action"], endpoint_table=endpoint_table,
                                                 capability=capability)

    @app.post("/internal/application/device-pairing")
    async def application_device_pairing(request: Request) -> JSONResponse:
        """application_device_pairing（桌面宿主配对控制）：仅受管桌面进程可管理主机信任。"""
        authorize_application(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="设备配对请求不是有效 JSON。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
            raise HTTPException(status_code=422, detail="设备配对操作格式无效。")
        operation = payload["operation"]
        manager = getattr(runtime, "device_pairing_manager", None)
        trust_store = getattr(runtime, "device_trust_store", None)
        if manager is None or trust_store is None:
            raise HTTPException(status_code=503, detail="设备配对存储尚未初始化。")
        try:
            if operation == "status" and set(payload) == {"operation"}:
                result = manager.status()
            elif operation == "open" and set(payload) == {"operation", "ttl_seconds"}:
                ttl_seconds = payload["ttl_seconds"]
                if type(ttl_seconds) is not int:
                    raise HTTPException(status_code=422, detail="配对窗口时长必须是整数秒。")
                code, expires_at_ms = manager.open_window(ttl_seconds=ttl_seconds)
                result = {"pairing_code": code, "window_expires_at_ms": expires_at_ms, **manager.status()}
            elif operation == "close" and set(payload) == {"operation"}:
                manager.close_window()
                result = manager.status()
            elif operation in {"approve", "reject"} and set(payload) == {"operation", "request_id"}:
                request_id = payload["request_id"]
                if (not isinstance(request_id, str) or len(request_id) != 32
                        or any(char not in "0123456789abcdef" for char in request_id)):
                    raise HTTPException(status_code=422, detail="待批准请求编号无效。")
                if operation == "approve":
                    record = manager.approve(request_id)
                    result = {"approved": True, "device_id": record.device_id,
                              "fingerprint": record.fingerprint, **manager.status()}
                else:
                    if not manager.reject(request_id):
                        raise HTTPException(status_code=404, detail="待批准请求不存在或已过期。")
                    result = {"rejected": True, **manager.status()}
            elif operation == "revoke" and set(payload) == {"operation", "device_id"}:
                device_id = payload["device_id"]
                if not isinstance(device_id, str) or not device_id:
                    raise HTTPException(status_code=422, detail="设备编号无效。")
                if not trust_store.revoke(device_id):
                    raise HTTPException(status_code=404, detail="可信设备不存在。")
                connection = runtime.device_registry.connection(device_id)
                if connection is not None and connection.connected:
                    # 撤销信任后立即使所有能力的受管会话失效，包括仅有音频流的客户端；在途结果由路由结算为未知。
                    await runtime.close_remote_device_session(device_id, connection.connection_generation)
                result = {"revoked": True, **manager.status()}
            else:
                raise HTTPException(status_code=422, detail="设备配对操作或字段无效。")
        except DeviceTrustError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:240]) from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def registry_or_404() -> ModelRegistryService:
        if model_registry is None:
            raise HTTPException(status_code=404, detail="模型管理服务尚未启用。")
        return model_registry

    def registry_error(exc: Exception) -> None:
        if isinstance(exc, CredentialVaultUnavailable):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, (ModelRegistryError, ProductSettingsError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise exc

    @app.get("/api/v1/product/snapshot")
    async def get_product_snapshot() -> dict[str, Any]:
        raw_runtime_snapshot = runtime.snapshot().payload
        runtime_payload = sanitize_runtime_snapshot(raw_runtime_snapshot)
        # Keep the response field for existing clients without reviving the
        # archived local synthesizer or inspecting its model directory.
        runtime_payload["tts_worker"] = {"state": "stopped"}
        conversation = _product_conversation_snapshot(
            runtime_payload.pop("conversation", {"recent": [], "current": {}}),
            runtime_current=raw_runtime_snapshot.get("current"),
        )
        settings = product_settings.snapshot() if product_settings is not None else {
            "schema_version": 12,
            "wake": {
                "enabled": runtime.config.wake.enabled,
                "keyword": runtime.config.wake.keyword,
                "keyword_label": runtime.config.wake.keyword_label,
                "keywords": list(runtime.config.wake.keywords),
                "sensitivity": _threshold_to_sensitivity(runtime.config.wake.threshold),
            },
            "silence": {
                "seconds": int(getattr(runtime.config.agent, "final_silence_ms", 30000) / 1000),
                "smart_turn_hard_silence_ms": runtime.config.smart_turn.hard_silence_ms,
            },
            "shadow": {
                "experimental_context_gate_enabled": (
                    runtime.config.shadow_agent.experimental_context_gate_enabled
                ),
            },
            "tts": {
                "backend": runtime.config.tts.backend,
                "local_model": runtime.config.tts.local_model,
            },
            "speaker": {
                "mode": runtime.config.speaker.mode,
                "confirm_threshold": runtime.config.speaker.confirm_threshold,
                "continuity_threshold": runtime.config.speaker.continuity_threshold,
                "continuity_relaxation": _speaker_relaxation(runtime.config.speaker),
                "intra_turn_relaxation": runtime.config.speaker.intra_turn_relaxation,
                "owner_only_barge_in": runtime.config.speaker.owner_only_barge_in,
            },
            "compute": {
                "devices": dict(runtime.config.compute.devices),
                "fallback_devices": dict(runtime.config.compute.fallback_devices),
            },
            "persona": {},
            "custom_instructions": [],
            "warnings": [],
        }
        models = await model_registry.snapshot() if model_registry is not None else {
            "providers": [], "models": [], "tts_profiles": [], "assignments": {},
        }
        restore_warnings = [
            str(item)[:320]
            for item in getattr(runtime, "startup_errors", [])
            if str(item).startswith("model_assignment_restore")
        ]
        if restore_warnings:
            settings["warnings"] = list(dict.fromkeys([
                *[str(item) for item in settings.get("warnings", [])],
                *restore_warnings,
            ]))
        return {
            "schema_version": 1,
            "runtime": runtime_payload,
            "screen_capture_available": bool(runtime.history.screen_capture.available and getattr(runtime.llm, "supports_images", False)),
            "conversation": conversation,
            "shadow": runtime._shadow_product_snapshot(),
            "dsh": runtime.dsh.status_payload(),
            "settings": settings,
            "models": models,
            "warnings": restore_warnings,
        }

    def device_snapshot() -> dict[str, Any]:
        """device_snapshot（设备状态快照）：界面与已授权代理使用同一来源。"""
        registry = getattr(runtime, "device_registry", None)
        if registry is None:
            raise HTTPException(status_code=503, detail="设备管理器尚未就绪。")
        snapshot, interaction_generation, active = registry.state_snapshot()
        devices = [{"device_id": item.device_id, "name": "本机" if item.device_id == "local" else item.device_id,
                    "connected": item.connected, "connection_generation": item.connection_generation,
                    "executor_generation": item.executor_generation,
                    "capabilities": sorted(set(item.capabilities)
                                     | ({"desktop_control", "terminal_control", "file_transfer"} if item.device_id == "local"
                                        and getattr(runtime, "_local_desktop_transport", None) is not None else set())
                                     | ({"screen_capture"} if item.device_id == "local"
                                        and runtime.history.screen_capture.available_for("local") else set())),
                    "error": None, "interaction_active": bool(active and active.device_id == item.device_id)}
                   for item in snapshot]
        return {"devices": devices,
                "active_device_id": active.device_id if active is not None else None,
                "interaction_generation": interaction_generation,
                "capability_labels": {
                    "audio_stream": "语音音频流", "client_tts": "客户端在线语音合成",
                    "desktop_control": "桌面观察与控制",
                    "terminal_control": "受限终端",
                    "file_transfer": "设备文件读写",
                    "screen_capture": "屏幕捕获"},
                "fields": {"active_device_id": "当前获主机授予语音交互租约的设备；空值表示未授予",
                           "interaction_generation": "当前语音占用代次；切换时递增",
                           "device_id": "设备编号；local 表示本机", "name": "设备名称",
                           "connected": "设备是否连接", "connection_generation": "连接代次",
                           "executor_generation": "执行器代次", "capabilities": "当前可用能力",
                           "error": "设备错误；当前尚无错误记录", "capability_labels": "能力编号中文说明",
                           "interaction_active": "是否为当前语音交互设备"}}

    @app.get("/api/v1/devices")
    async def get_devices() -> JSONResponse:
        """get_devices（读取设备）：仅返回状态，不暴露配对信息或凭据。"""
        return JSONResponse(device_snapshot(), headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/devices/interaction")
    async def select_interaction_device(payload: dict[str, Any]) -> JSONResponse:
        """select_interaction_device（手动切换语音设备）：准备后待目标设备完成媒体就绪。"""
        expected = {"device_id", "expected_interaction_generation"}
        if (set(payload) != expected or not isinstance(payload.get("device_id"), str)
                or type(payload.get("expected_interaction_generation")) is not int
                or payload["expected_interaction_generation"] < 0):
            raise HTTPException(status_code=422, detail="语音设备切换请求字段无效。")
        try:
            lease = await runtime.prepare_interaction_switch(
                payload["device_id"], payload["expected_interaction_generation"],
            )
            if lease.device_id == "local" and runtime.device_registry.pending == lease:
                await runtime.complete_local_interaction_switch(lease)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="语音设备状态已变化，请刷新设备列表后重试。") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail="目标设备未就绪或另一个切换事务正在进行。") from exc
        return JSONResponse({
            "protocol_version": 1,
            "result": "active" if runtime.device_registry.accepts(lease) else "pending_media",
            "device_id": lease.device_id,
            "core_session_id": runtime.core_session_id,
            "connection_generation": lease.connection_generation,
            "executor_generation": lease.executor_generation,
            "interaction_generation": lease.interaction_generation,
            "fields": {
                "protocol_version": "语音切换协议版本",
                "result": "已经活动或等待目标媒体连接就绪",
                "device_id": "用户明确选择的语音设备编号",
                "core_session_id": "权威核心运行编号",
                "connection_generation": "目标设备连接代次",
                "executor_generation": "目标设备执行器代次",
                "interaction_generation": "主机准备授予的新语音占用代次",
            },
        }, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/dsh/status")
    async def get_dsh_status() -> dict[str, object]:
        return runtime.dsh.status_payload()

    @app.post("/api/v1/dsh/ui-launch")
    async def launch_dsh_ui(request: Request) -> JSONResponse:
        if request.headers.get("origin") not in origins:
            raise HTTPException(status_code=403, detail="不允许从该 Origin 打开 DSH 控制台。")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="DSH 启动接口只接受 application/json。")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体必须是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象。")
        status = runtime.dsh.status()
        if status.state == "starting":
            raise HTTPException(status_code=409, detail="DSH 控制台仍在启动。")
        launch_url = runtime.dsh.launch_url
        if status.state != "ready" or not launch_url:
            raise HTTPException(status_code=503, detail=status.message or "DSH 控制台不可用。")
        return JSONResponse(
            {"url": launch_url},
            headers={"Cache-Control": "no-store"},
        )

    async def dsh_callback_body(
        request: Request, *, allow_terminal_replay: bool = False,
    ) -> tuple[dict[str, Any], DshShadowAgentSession]:
        remote = request.client.host if request.client is not None else None
        if not runtime.dsh.bridge_hub.authorize(request.headers.get("authorization"), remote):
            raise HTTPException(status_code=401, detail="unauthorized")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="application/json required")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
            raise HTTPException(status_code=409, detail="protocol mismatch")
        session = runtime.dsh.bridge_hub.resolve(payload)
        if session is None:
            raise HTTPException(status_code=404, detail="binding not found")
        await session.prepare_callback_binding(payload)
        if (payload.get("dsh_session_id") != session.dsh_session_id
                or payload.get("epoch") != session.epoch):
            if not (allow_terminal_replay and session.matches_last_terminal_identity(payload)):
                raise HTTPException(status_code=409, detail="stale binding")
        return payload, session

    @app.post("/api/internal/dsh-bridge/shadow.event")
    async def dsh_shadow_event(request: Request) -> JSONResponse:
        payload, session = await dsh_callback_body(request, allow_terminal_replay=True)
        code = await session.accept_event(payload)
        return JSONResponse(
            {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": code},
            status_code=200 if code in {"accepted", "duplicate"} else 409,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/internal/dsh-bridge/attachment.read")
    async def dsh_attachment_read(request: Request) -> JSONResponse:
        """attachment.read（程序内部取图）：验证绑定，不触发模型工具或历史浏览。"""
        remote = request.client.host if request.client is not None else None
        if not runtime.dsh.bridge_hub.authorize(request.headers.get("authorization"), remote):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="图片读取请求不是有效 JSON（结构化数据）") from exc
        if not isinstance(payload, dict) or payload.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
            raise HTTPException(status_code=409, detail="protocol mismatch")
        session = runtime.dsh.bridge_hub.resolve(payload)
        if session is None or payload.get("epoch") != session.epoch or payload.get("dsh_session_id") != session.dsh_session_id:
            raise HTTPException(status_code=409, detail="stale binding")
        aid = payload.get("attachment_id")
        if not isinstance(aid, str):
            raise HTTPException(status_code=400, detail="附件标识无效")
        item = runtime.history.screen_attachment(session.voice_session_id, aid)
        if item is None:
            raise HTTPException(status_code=404, detail="当前会话附件不存在")
        return JSONResponse({"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted", "attachment": item},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/internal/dsh-bridge/device.list")
    async def dsh_device_list(request: Request) -> JSONResponse:
        """device.list（设备列表）：仅向当前绑定且活跃的根代理返回设备状态。"""
        payload, session = await dsh_callback_body(request)
        activation_id = payload.get("activation_id")
        if not isinstance(activation_id, str) or not activation_id or session.activation_id != activation_id:
            raise HTTPException(status_code=409, detail="设备列表请求所属激活已失效。")
        return JSONResponse({"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted", **device_snapshot()},
                            headers={"Cache-Control": "no-store"})

    def dsh_device_id(payload: dict[str, Any], session: DshShadowAgentSession, *, allow_omitted: bool = False) -> str:
        """dsh_device_id（验证设备编号）：校验回调所属激活和明确的设备目标。"""
        activation_id = payload.get("activation_id")
        if not isinstance(activation_id, str) or not activation_id or session.activation_id != activation_id:
            raise HTTPException(status_code=409, detail="设备请求所属 DSH 激活已失效。")
        device_id = payload.get("device_id")
        if allow_omitted and device_id is None:
            device_id = "local"
        if not isinstance(device_id, str) or not device_id or len(device_id) > 128 or device_id != device_id.strip():
            raise HTTPException(status_code=422, detail="设备编号无效。")
        return device_id

    async def dsh_device_target(payload: dict[str, Any], session: DshShadowAgentSession, *,
                               allow_omitted: bool = False, required_capability: str = "desktop_control"):
        device_id = dsh_device_id(payload, session, allow_omitted=allow_omitted)
        connection = runtime.device_registry.connection(device_id)
        if connection is None or not connection.connected:
            raise HTTPException(status_code=503, detail="固定目标设备未连接；不会改投本机。")
        if device_id != "local":
            if required_capability not in connection.capabilities:
                raise HTTPException(status_code=403, detail="固定目标设备未声明所需的受管能力。")
            if runtime.device_router is None or not await runtime.device_router.has_transport(device_id):
                raise HTTPException(status_code=503, detail="固定目标设备没有有效的已认证执行通道；不会改投本机。")
        return connection

    def valid_device_request_id(value: Any) -> bool:
        return (isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()
                and value[0].isalnum() and all(char.isalnum() or char in "._:-" for char in value))

    @app.post("/api/internal/dsh-bridge/device.windows")
    async def dsh_device_windows(request: Request) -> JSONResponse:
        """device.windows（本机窗口清单）：只读取窗口身份，不读取窗口像素。"""
        payload, session = await dsh_callback_body(request)
        if set(payload) != {"protocol_version", "request_id", "voice_session_id", "dsh_session_id", "epoch",
                            "context_revision", "activation_id", "device_id"}:
            raise HTTPException(status_code=422, detail="窗口清单请求字段无效。")
        connection = await dsh_device_target(payload, session)
        if not valid_device_request_id(payload.get("request_id")):
            raise HTTPException(status_code=422, detail="窗口清单请求编号无效。")
        try:
            record = await runtime.execute_device_task(request_id=payload["request_id"],
                device_id=connection.device_id, operation="windows", arguments={}, timeout_seconds=15.0)
        except Exception as exc:
            status = 504 if isinstance(exc, DeviceOutcomeUnknown) else 503
            raise HTTPException(status_code=status, detail="固定目标设备窗口清单未取得可靠回执。") from exc
        if record.status == "unknown":
            raise HTTPException(status_code=504, detail="固定目标设备窗口清单结果未知；请查询原任务。")
        if record.status != "succeeded" or not isinstance(record.result, list):
            raise HTTPException(status_code=503, detail="固定目标设备窗口清单回执无效。")
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="窗口读取期间 DSH 激活已结束。")
        return JSONResponse({"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted",
            "device_id": connection.device_id, "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation, "windows": record.result,
            "fields":{"device_id":"固定执行设备编号","connection_generation":"设备连接代次",
                "executor_generation":"设备执行器代次","windows":"固定目标设备的可见窗口与进程身份清单"}},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/internal/dsh-bridge/device.observe")
    async def dsh_device_observe(request: Request) -> JSONResponse:
        """device.observe（固定目标设备窗口观察）：读取 UI Automation（界面自动化）树并绑定设备代次。"""
        payload, session = await dsh_callback_body(request)
        required_fields = {"protocol_version", "request_id", "voice_session_id", "dsh_session_id", "epoch",
                           "context_revision", "activation_id", "device_id", "window_id", "max_elements", "max_depth", "include_pixels"}
        optional_generations = {"connection_generation", "executor_generation"}
        if (not required_fields.issubset(payload) or set(payload) - required_fields - optional_generations
                or ("connection_generation" in payload) != ("executor_generation" in payload)):
            raise HTTPException(status_code=422, detail="设备观察请求字段无效。")
        connection = await dsh_device_target(payload, session)
        if ("connection_generation" in payload
                and (payload["connection_generation"] != connection.connection_generation
                     or payload["executor_generation"] != connection.executor_generation)):
            raise HTTPException(status_code=409, detail="设备观察所属连接或执行器代次已失效。")
        if not valid_device_request_id(payload.get("request_id")):
            raise HTTPException(status_code=422, detail="设备观察请求编号无效。")
        window_id, max_elements, max_depth, include_pixels = (
            payload.get("window_id"), payload.get("max_elements"), payload.get("max_depth"), payload.get("include_pixels"))
        if (type(window_id) is not int or window_id <= 0 or type(max_elements) is not int or not 1 <= max_elements <= 200
                or type(max_depth) is not int or not 1 <= max_depth <= 16 or type(include_pixels) is not bool):
            raise HTTPException(status_code=422, detail="窗口编号或观察范围无效。")
        service = runtime.history.screen_capture
        if not service.config.enabled:
            raise HTTPException(status_code=503, detail="屏幕观察能力当前关闭。")
        if connection.device_id == "local" and not service.available_for("local"):
            raise HTTPException(status_code=503, detail="本机屏幕观察能力当前不可用。")
        arguments = {"target":{"windowId":window_id},"maxElements":max_elements,"maxDepth":max_depth,
                     "includePixels":include_pixels}
        try:
            record = await runtime.execute_device_task(request_id=payload["request_id"],
                device_id=connection.device_id, operation="tree", arguments=arguments, timeout_seconds=20.0)
        except Exception as exc:
            status = 504 if isinstance(exc, DeviceOutcomeUnknown) else 503
            raise HTTPException(status_code=status, detail="固定目标设备窗口观察未取得可靠回执。") from exc
        if record.status == "unknown":
            raise HTTPException(status_code=504, detail="固定目标设备观察结果未知；请查询原任务。")
        value = record.result if record.status == "succeeded" else None
        snapshot = value.get("snapshot") if isinstance(value, dict) else None
        elements = value.get("elements") if isinstance(value, dict) else None
        pixels = value.get("pixels") if isinstance(value, dict) else None
        if not isinstance(snapshot, dict) or not isinstance(elements, list) or not isinstance(pixels, list):
            raise HTTPException(status_code=503, detail="固定目标设备窗口观察回执无效。")
        required_snapshot = {"windowId", "processId", "executablePath", "title", "className", "rect",
                             "foreground", "treeHash", "shotHash", "elementCount"}
        rect = snapshot.get("rect") if isinstance(snapshot, dict) else None
        if (set(snapshot) != required_snapshot or type(snapshot.get("windowId")) is not int
                or type(snapshot.get("processId")) is not int
                or snapshot.get("executablePath") is not None and not isinstance(snapshot.get("executablePath"), str)
                or not isinstance(snapshot.get("title"), str) or not isinstance(snapshot.get("className"), str)
                or not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}
                or any(type(rect.get(key)) is not int for key in ("x", "y", "width", "height"))
                or type(snapshot.get("foreground")) is not bool or not isinstance(snapshot.get("treeHash"), str)
                or not isinstance(snapshot.get("shotHash"), str) or type(snapshot.get("elementCount")) is not int):
            raise HTTPException(status_code=503, detail="固定目标设备窗口身份快照无效。")
        if (session.activation_id != payload["activation_id"]
                or runtime.device_registry.connection(connection.device_id) != connection):
            raise HTTPException(status_code=409, detail="观察期间 DSH 激活或设备代次已变化。")
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted","device_id":connection.device_id,
            "connection_generation":connection.connection_generation,"executor_generation":connection.executor_generation,
            "snapshot":snapshot,"elements":elements,"pixels":pixels,
            "fields":{"device_id":"固定执行设备编号","connection_generation":"设备连接代次",
                "executor_generation":"设备执行器代次","snapshot":"已核对进程身份及窗口内容摘要",
                "elements":"有界界面自动化元素树","pixels":"有界元素中心像素提示"}},
            headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/device.action")
    async def dsh_device_action(request: Request) -> JSONResponse:
        """device.action（已审批的固定目标设备动作）：固定原观察目标并进入持久设备任务台账。"""
        payload, session = await dsh_callback_body(request)
        expected = {"protocol_version","request_id","voice_session_id","dsh_session_id","epoch","context_revision",
                    "activation_id","device_id","connection_generation","executor_generation","operation","arguments","timeout_ms"}
        if set(payload) != expected:
            raise HTTPException(status_code=422, detail="设备动作请求字段无效。")
        connection = await dsh_device_target(payload, session)
        if not valid_device_request_id(payload.get("request_id")):
            raise HTTPException(status_code=422, detail="设备动作任务编号无效。")
        operation, arguments, timeout_ms = payload.get("operation"), payload.get("arguments"), payload.get("timeout_ms")
        allowed = {"click", "type", "scroll", "key", "move", "windowControl", "invokePattern", "multi_action"}
        if (operation not in allowed or not isinstance(arguments, dict) or type(timeout_ms) is not int
                or not 1 <= timeout_ms <= 600_000):
            raise HTTPException(status_code=422, detail="设备动作名称、参数或时限无效。")
        if (payload.get("connection_generation") != connection.connection_generation
                or payload.get("executor_generation") != connection.executor_generation):
            raise HTTPException(status_code=409, detail="设备动作观察所属代次已失效。")
        try:
            record = await runtime.execute_device_task(request_id=payload["request_id"],
                device_id=connection.device_id, operation=operation, arguments=arguments,
                timeout_seconds=min(600.0, timeout_ms / 1000 + 8), expected_connection=connection)
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail="设备动作任务编号已用于其他内容。") from exc
        except DeviceTargetChanged as exc:
            raise HTTPException(status_code=409, detail="设备动作观察所属代次已失效，动作未派发。") from exc
        except DeviceOutcomeUnknown as exc:
            try:
                record = await runtime.query_device_task(payload["request_id"], connection.device_id)
            except Exception:
                record = None
            if record is None:
                raise HTTPException(status_code=504,
                    detail=f"设备动作结果未知，请查询原任务 {payload['request_id']}。") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="设备动作尚未取得可靠回执。") from exc
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="动作返回期间 DSH 激活已结束；任务仍固定在原设备。")
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted","task":{
            "request_id":record.request_id,"device_id":record.device_id,"operation":record.operation,
            "status":record.status,"result":record.result},
            "fields":{"task":"原设备动作任务台账结果；未知状态不得自动重做"}},
            headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/device.task.query")
    async def dsh_device_task_query(request: Request) -> JSONResponse:
        """device.task.query（只读原任务查询）：不会重新派发桌面操作。"""
        payload, session = await dsh_callback_body(request)
        if set(payload) != {"protocol_version","request_id","voice_session_id","dsh_session_id","epoch",
                            "context_revision","activation_id","device_id","task_id"}:
            raise HTTPException(status_code=422, detail="设备任务查询字段无效。")
        device_id = dsh_device_id(payload, session)
        task_id = payload.get("task_id")
        if not valid_device_request_id(task_id):
            raise HTTPException(status_code=422, detail="设备任务编号无效。")
        try:
            record = await runtime.query_device_task(task_id, device_id)
        except TaskConflict:
            # 原记录归属其他设备时，与该设备下没有记录使用同一回执。
            record = None
        except Exception as exc:
            raise HTTPException(status_code=503, detail="设备任务台账不可用。") from exc
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="任务查询期间 DSH 激活已结束。")
        task = None if record is None else {"request_id":record.request_id,"device_id":record.device_id,
            "operation":record.operation,"status":record.status,"result":record.result}
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted","task":task,
            "fields":{"task":"原任务状态；查询不会重放操作"}},headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/device.terminal")
    async def dsh_device_terminal(request: Request) -> JSONResponse:
        """device.terminal（受限设备终端）：只派发固定命令配置，不提供 shell 或脚本入口。"""
        payload, session = await dsh_callback_body(request)
        base = {"protocol_version", "request_id", "voice_session_id", "dsh_session_id", "epoch",
                "context_revision", "activation_id", "device_id", "action", "terminal_id"}
        action = payload.get("action")
        if not isinstance(action, str) or action not in {"create", "input", "output", "status", "cancel", "close"}:
            raise HTTPException(status_code=422, detail="设备终端请求字段或操作无效。")
        extra = {"input": {"command_id"}, "output": {"offset"}}.get(action, set())
        if set(payload) != base | extra:
            raise HTTPException(status_code=422, detail="设备终端请求字段或操作无效。")
        connection = await dsh_device_target(payload, session, required_capability="terminal_control")
        request_id, terminal_id = payload.get("request_id"), payload.get("terminal_id")
        if not valid_device_request_id(request_id) or not valid_device_request_id(terminal_id):
            raise HTTPException(status_code=422, detail="设备终端请求编号无效。")
        if action == "input" and (not isinstance(payload.get("command_id"), str)
                                   or payload["command_id"] not in {"hostname", "identity", "processes", "network"}):
            raise HTTPException(status_code=422, detail="设备终端命令不在固定只读命令清单中。")
        if action == "output" and (type(payload.get("offset")) is not int or payload["offset"] < 0):
            raise HTTPException(status_code=422, detail="设备终端输出偏移无效。")
        arguments = {"terminal_id": terminal_id}
        if action == "input":
            arguments["command_id"] = payload["command_id"]
        elif action == "output":
            arguments["offset"] = payload["offset"]
        try:
            record = await runtime.execute_device_task(request_id=request_id,
                device_id=connection.device_id, operation=f"terminal.{action}", arguments=arguments,
                timeout_seconds=45.0 if action == "input" else 15.0)
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail="设备终端任务编号已用于其他内容。") from exc
        except DeviceOutcomeUnknown as exc:
            try:
                record = await runtime.query_device_task(request_id, connection.device_id)
            except Exception:
                record = None
            if record is None or record.status == "unknown":
                raise HTTPException(status_code=504,
                    detail=f"设备终端任务结果未知，请查询原任务 {request_id}，不要重放。") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="固定目标设备终端通道不可用；不会改投本机。") from exc
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="终端任务返回期间 DSH 激活已结束；任务仍固定在原设备。")
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
            "task":{"request_id":record.request_id,"device_id":record.device_id,"operation":record.operation,
                "status":record.status,"result":record.result},
            "fields":{"task":"固定设备上的终端会话或命令任务结果；未知状态不得重放"}},
            headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/device.file")
    async def dsh_device_file(request: Request) -> JSONResponse:
        """device.file（设备文件）：目录、小/大文件读取与获批文件写入；远端大文件走固定 FILE 通道。"""
        payload, session = await dsh_callback_body(request)
        base = {"protocol_version", "request_id", "voice_session_id", "dsh_session_id", "epoch",
                "context_revision", "activation_id", "device_id", "action", "path"}
        action = payload.get("action")
        if not isinstance(action, str) or action not in {"list", "read", "write"}:
            raise HTTPException(status_code=422, detail="设备文件请求操作无效。")
        extra = {"limit"} if action == "list" else {"max_bytes"} if action == "read" else {"attachment"}
        if set(payload) != base | extra:
            raise HTTPException(status_code=422, detail="设备文件请求字段无效。")
        connection = await dsh_device_target(payload, session, required_capability="file_transfer")
        request_id, path = payload.get("request_id"), payload.get("path")
        if (not valid_device_request_id(request_id) or not isinstance(path, str) or not path
                or len(path) > 2048 or any(ord(character) < 32 for character in path)):
            raise HTTPException(status_code=422, detail="设备文件请求编号或目标路径无效。")
        if action == "list":
            if type(payload.get("limit")) is not int or not 1 <= payload["limit"] <= 200:
                raise HTTPException(status_code=422, detail="设备目录清单上限必须为 1 至 200。")
            arguments = {"path": path, "limit": payload["limit"]}
        elif action == "read":
            from ..devices.file_transfer import MAX_TRANSFER_BYTES
            if type(payload.get("max_bytes")) is not int or not 1 <= payload["max_bytes"] <= MAX_TRANSFER_BYTES:
                raise HTTPException(status_code=422, detail="设备文件读取上限必须为 1 至 4 GiB。")
            arguments = {"path": path, "max_bytes": min(payload["max_bytes"], 128 * 1024)}
        else:
            attachment = payload.get("attachment")
            if (not isinstance(attachment, dict) or set(attachment) != {"attachment_id", "name", "bytes"}
                    or not isinstance(attachment.get("attachment_id"), str)
                    or not isinstance(attachment.get("name"), str)
                    or len(attachment["name"]) > 240
                    or type(attachment.get("bytes")) is not int):
                raise HTTPException(status_code=422, detail="DSH 文件附件引用字段无效。")
            digest_value = attachment["attachment_id"].removeprefix("sha256:")
            from ..devices.file_transfer import MAX_TRANSFER_BYTES
            import re
            if (not re.fullmatch(r"[0-9a-f]{64}", digest_value)
                    or not 0 <= attachment["bytes"] <= MAX_TRANSFER_BYTES):
                raise HTTPException(status_code=422, detail="DSH 文件附件长度或内容摘要无效。")
            store = runtime.device_file_transfer_store
            if store is None:
                raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
            transfer_id = device_file_transfer_id(connection, request_id)
            try:
                ticket = await asyncio.to_thread(store.prepare_core_upload,
                    device_id=connection.device_id, connection_generation=connection.connection_generation,
                    executor_generation=connection.executor_generation, expected_bytes=attachment["bytes"],
                    expected_sha256=digest_value, target_path=path, transfer_id=transfer_id)
            except Exception as exc:
                raise HTTPException(status_code=503, detail="设备文件暂存未能建立。") from exc
            if session.activation_id != payload["activation_id"]:
                raise HTTPException(status_code=409, detail="文件暂存建立期间 DSH 激活已结束。")
            return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
                "task":{"request_id":request_id,"device_id":connection.device_id,"operation":"file.write",
                    "status":"receiving","result":{"transfer_id":transfer_id,"bytes":attachment["bytes"],
                        "sha256":digest_value,"name":attachment["name"],
                        "path":path,
                        "connection_generation":connection.connection_generation,
                        "executor_generation":connection.executor_generation}},
                "fields":{"task":"已审批的文件写入暂存票据；分块核验完成前不会派发到设备"}},
                headers={"Cache-Control":"no-store"})

        async def execute_file_task(task_request_id: str, operation: str, task_arguments: dict[str, Any],
                                    timeout_seconds: float = 30.0):
            try:
                record = await runtime.execute_device_task(request_id=task_request_id,
                    device_id=connection.device_id, operation=operation, arguments=task_arguments,
                    timeout_seconds=timeout_seconds)
                return record
            except Exception as exc:
                if isinstance(exc, TaskConflict):
                    raise HTTPException(status_code=409, detail="设备文件任务编号已用于其他内容。") from exc
                if isinstance(exc, DeviceOutcomeUnknown):
                    try:
                        return await runtime.query_device_task(task_request_id, connection.device_id)
                    except Exception:
                        return None
                if isinstance(exc, (DeviceUnavailable, RuntimeError)):
                    raise HTTPException(status_code=503, detail="固定目标设备文件通道不可用；不会改投本机。") from exc
                raise

        if action == "list":
            record = await execute_file_task(request_id, "file.list", arguments)
            task_id = request_id
            operation = "file.list"
            result = None
        else:
            stat_id = device_file_subtask_id(request_id, "stat")
            stat_record = await execute_file_task(stat_id, "file.stat", {"path": path})
            if stat_record is None or stat_record.status != "succeeded" or not isinstance(stat_record.result, dict):
                record = stat_record
                task_id = stat_id
                operation = "file.stat"
                result = None
            else:
                stat = stat_record.result
                size = stat.get("bytes")
                if (stat.get("path") != path or not isinstance(stat.get("name"), str)
                        or type(size) is not int or size < 0):
                    raise HTTPException(status_code=502, detail="设备文件状态回执格式无效。")
                if size > payload["max_bytes"]:
                    return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
                        "task":{"request_id":request_id,"device_id":connection.device_id,"operation":"file.read",
                            "status":"failed","result":{"error":"文件超过本次用户批准的字节上限。",
                                "bytes":size,"max_bytes":payload["max_bytes"]}},
                        "fields":{"task":"设备文件读取结果；文件大小超过审批上限时不读取内容"}},
                        headers={"Cache-Control":"no-store"})
                if size <= 128 * 1024:
                    read_id = device_file_subtask_id(request_id, "read")
                    record = await execute_file_task(read_id, "file.read", {"path":path,"max_bytes":max(1, size)})
                    task_id = read_id
                    operation = "file.read"
                    result = None
                else:
                    store = runtime.device_file_transfer_store
                    if store is None:
                        raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
                    transfer_id = device_file_transfer_id(connection, request_id)
                    try:
                        if connection.device_id == "local":
                            await asyncio.to_thread(store.prepare_local_upload,
                                device_id=connection.device_id, connection_generation=connection.connection_generation,
                                executor_generation=connection.executor_generation, expected_bytes=size,
                                transfer_id=transfer_id)
                            staged = await asyncio.to_thread(store.stage_local_file,
                                transfer_id=transfer_id, device_id=connection.device_id,
                                connection_generation=connection.connection_generation,
                                executor_generation=connection.executor_generation, source_path=path)
                        else:
                            ticket = await asyncio.to_thread(store.prepare_device_upload,
                                device_id=connection.device_id, connection_generation=connection.connection_generation,
                                executor_generation=connection.executor_generation, expected_bytes=size,
                                transfer_id=transfer_id)
                            sftp_id = device_file_subtask_id(request_id, "sftp")
                            record = await execute_file_task(sftp_id, "file.sftp.upload", {
                                "local_path":path,"transfer_id":transfer_id,"parent_request_id":request_id,
                                "maximum_bytes":max(1,size)}, timeout_seconds=600.0)
                            task_id, operation, result = sftp_id, "file.sftp.upload", None
                            if record is None or record.status == "unknown":
                                response_task = {"request_id":request_id,"device_id":connection.device_id,
                                    "operation":"file.read","status":"unknown","result":None,
                                    "source_task_id":sftp_id,"transfer_id":transfer_id}
                                return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
                                    "task":response_task,"fields":{"task":"固定 FILE 原任务结果未知；查询 source_task_id，不得重放"}},
                                    headers={"Cache-Control":"no-store"})
                            if record.status != "succeeded" or not isinstance(record.result, dict):
                                await asyncio.to_thread(store.close, transfer_id=transfer_id,
                                    device_id=connection.device_id, connection_generation=connection.connection_generation,
                                    executor_generation=connection.executor_generation)
                                return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
                                    "task":{"request_id":request_id,"device_id":connection.device_id,"operation":"file.read",
                                        "status":"failed","result":record.result if record is not None else None},
                                    "fields":{"task":"固定设备 FILE 读取失败；不会改投本机"}},
                                    headers={"Cache-Control":"no-store"})
                            receipt = record.result
                            if (receipt.get("transfer_id") != transfer_id
                                    or receipt.get("path") != f"transfer:{transfer_id}"
                                    or receipt.get("bytes") != size or not isinstance(receipt.get("sha256"), str)):
                                raise HTTPException(status_code=502, detail="远端 FILE 原任务回执与暂存票据不匹配。")
                            staged = await asyncio.to_thread(store.verify_device_upload,
                                transfer_id=transfer_id, device_id=connection.device_id,
                                connection_generation=connection.connection_generation,
                                executor_generation=connection.executor_generation, expected_bytes=size,
                                expected_sha256=receipt["sha256"])
                    except HTTPException:
                        raise
                    except Exception as exc:
                        raise HTTPException(status_code=503, detail="设备大文件暂存或摘要校验失败。") from exc
                    result = {"path":path,"name":stat["name"],"bytes":staged["bytes"],
                        "sha256":staged["sha256"],"transfer_id":transfer_id,"streamed":True,
                        "connection_generation":connection.connection_generation,
                        "executor_generation":connection.executor_generation}
                    record = stat_record
                    task_id, operation = stat_id, "file.read"
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="文件任务返回期间 DSH 激活已结束；任务仍固定在原设备。")
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
            "task":{"request_id":request_id,"device_id":connection.device_id,"operation":operation,
                "status":record.status if record is not None else "unknown",
                "result":record.result if result is None and record is not None else result,
                **({"source_task_id":task_id} if record is None or record.status == "unknown" else {})},
            "fields":{"task":"固定设备文件任务结果；未知状态必须按 source_task_id 查询且不得重放"}},
            headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/device.file.transfer")
    async def dsh_device_file_transfer(request: Request) -> JSONResponse:
        """device.file.transfer（文件分块回调）：只为当前 DSH 激活读写票据对应的有界数据。"""
        payload, session = await dsh_callback_body(request)
        base = {"protocol_version", "request_id", "voice_session_id", "dsh_session_id", "epoch",
                "context_revision", "activation_id", "device_id", "connection_generation",
                "executor_generation", "parent_request_id", "transfer_id", "action"}
        action = payload.get("action")
        extras = {"read_chunk": {"offset", "limit"},
            "upload_chunk": {"offset", "data_base64"}, "finish_upload": {"expected_bytes", "expected_sha256"},
            "close": set()}
        if not isinstance(action, str) or action not in extras or set(payload) != base | extras[action]:
            raise HTTPException(status_code=422, detail="文件传输分块请求字段或操作无效。")
        connection = await dsh_device_target(payload, session, required_capability="file_transfer")
        if (not valid_device_request_id(payload.get("request_id"))
                or not valid_device_request_id(payload.get("parent_request_id"))
                or type(payload.get("connection_generation")) is not int
                or payload["connection_generation"] != connection.connection_generation
                or type(payload.get("executor_generation")) is not int
                or payload["executor_generation"] != connection.executor_generation
                or payload.get("transfer_id") != device_file_transfer_id(connection, payload["parent_request_id"])):
            raise HTTPException(status_code=409, detail="文件传输票据与原请求、设备或代次不匹配。")
        store = runtime.device_file_transfer_store
        if store is None:
            raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
        key = {"transfer_id":payload["transfer_id"],"device_id":connection.device_id,
               "connection_generation":connection.connection_generation,
               "executor_generation":connection.executor_generation}
        try:
            if action == "read_chunk":
                if type(payload["offset"]) is not int or type(payload["limit"]) is not int:
                    raise ValueError("文件分块读取偏移或上限无效。")
                result = await asyncio.to_thread(store.read_chunk, **key, offset=payload["offset"], limit=payload["limit"])
            elif action == "upload_chunk":
                if type(payload["offset"]) is not int or not isinstance(payload["data_base64"], str):
                    raise ValueError("文件分块上传字段无效。")
                result = await asyncio.to_thread(store.append_core_chunk, **key,
                    offset=payload["offset"], data_base64=payload["data_base64"])
            elif action == "finish_upload":
                if type(payload["expected_bytes"]) is not int or not isinstance(payload["expected_sha256"], str):
                    raise ValueError("附件完成摘要字段无效。")
                finished = await asyncio.to_thread(store.finish_core_upload, **key,
                    expected_bytes=payload["expected_bytes"], expected_sha256=payload["expected_sha256"])
                if session.activation_id != payload["activation_id"]:
                    raise HTTPException(status_code=409, detail="附件校验期间 DSH 激活已结束。")
                source_task_id: str | None = None
                if connection.device_id == "local":
                    result = await asyncio.to_thread(store.commit_local_file, **key,
                        target_path=finished["target_path"])
                    status = "succeeded"
                else:
                    ticket = await asyncio.to_thread(store.device_file_download_ticket, **key)
                    source_task_id = device_file_subtask_id(payload["parent_request_id"], "sftp")
                    task_arguments = {"local_path":ticket["target_path"],
                        "transfer_id":payload["transfer_id"],"parent_request_id":payload["parent_request_id"],
                        "sha256":ticket["sha256"],"maximum_bytes":max(1,ticket["bytes"])}
                    try:
                        task_record = await runtime.execute_device_task(request_id=source_task_id,
                            device_id=connection.device_id, operation="file.sftp.download",
                            arguments=task_arguments, timeout_seconds=600.0)
                    except Exception as exc:
                        from ..devices.task_journal import TaskConflict
                        from ..devices.router import DeviceOutcomeUnknown, DeviceUnavailable
                        if isinstance(exc, TaskConflict):
                            raise HTTPException(status_code=409, detail="设备文件写入编号已绑定其他任务内容。") from exc
                        if isinstance(exc, DeviceOutcomeUnknown):
                            task_record = await runtime.query_device_task(source_task_id, connection.device_id)
                        elif isinstance(exc, (DeviceUnavailable, RuntimeError)):
                            raise HTTPException(status_code=503, detail="固定目标设备文件写入通道不可用；不会改投本机。") from exc
                        else:
                            raise
                    if task_record is None or task_record.status == "unknown":
                        status, result = "unknown", None
                    elif task_record.status != "succeeded" or not isinstance(task_record.result, dict):
                        status, result = "failed", task_record.result
                    else:
                        receipt = task_record.result
                        if (receipt.get("transfer_id") != payload["transfer_id"]
                                or receipt.get("path") != f"transfer:{payload['transfer_id']}"
                                or receipt.get("bytes") != ticket["bytes"]
                                or receipt.get("sha256") != ticket["sha256"]):
                            raise HTTPException(status_code=502, detail="设备 FILE 写入回执与审批附件摘要不匹配。")
                        status, result = "succeeded", {"path":ticket["target_path"],"bytes":ticket["bytes"],
                            "sha256":ticket["sha256"],"transfer_id":payload["transfer_id"]}
                if session.activation_id != payload["activation_id"]:
                    raise HTTPException(status_code=409, detail="文件写入返回期间 DSH 激活已结束；请查询原任务。")
                return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted",
                    "task":{"request_id":payload["parent_request_id"],"device_id":connection.device_id,
                        "operation":"file.write","status":status,"result":result,
                        **({"source_task_id":source_task_id} if status == "unknown" and source_task_id else {})},
                    "fields":{"task":"已审批文件写入原任务；未知状态须按 source_task_id 查询，不得重放"}},
                    headers={"Cache-Control":"no-store"})
            else:
                result = {"closed": await asyncio.to_thread(store.close, **key)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail="文件传输票据状态、代次或内容校验失败。") from exc
        if session.activation_id != payload["activation_id"]:
            raise HTTPException(status_code=409, detail="文件分块返回期间 DSH 激活已结束。")
        return JSONResponse({"protocol_version":BRIDGE_PROTOCOL_VERSION,"result":"accepted","transfer":result,
            "fields":{"transfer":"固定设备、代次和原请求绑定的分块收据"}},headers={"Cache-Control":"no-store"})

    @app.post("/api/internal/dsh-bridge/screen.capture")
    async def dsh_screen_capture(request: Request) -> JSONResponse:
        """screen.capture（DSH 按需截屏）：限于当前有效激活，并复用唯一屏幕捕获服务。"""
        payload, session = await dsh_callback_body(request)
        activation_id = payload.get("activation_id")
        if not isinstance(activation_id, str) or not activation_id:
            raise HTTPException(status_code=422, detail="截图请求缺少有效的激活标识。")
        if session.activation_id != activation_id:
            raise HTTPException(status_code=409, detail="截图请求所属激活已失效，请等待当前任务状态刷新。")
        service = runtime.history.screen_capture
        device_id = payload.get("device_id")
        if device_id is not None and (not isinstance(device_id, str) or not device_id
                                      or len(device_id) > 128 or device_id != device_id.strip()):
            raise HTTPException(status_code=422, detail="截图目标设备编号无效。")
        if not service.config.enabled:
            raise HTTPException(status_code=409, detail="屏幕图像设置已关闭，无法捕获屏幕。")
        if not service.available_for(device_id):
            raise HTTPException(status_code=503, detail="屏幕捕获组件当前不可用。")
        generation = service.generation
        lease_generation = service.lease_generation("dsh", activation_id)
        try:
            packet = await service.grab(source="dsh", scope=activation_id, device_id=device_id)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="屏幕捕获超过 3 秒时限。") from exc
        except RuntimeError as exc:
            status = 409 if str(exc) in {"屏幕捕获当前不可用", "上一次屏幕捕获尚未结束"} else 503
            raise HTTPException(status_code=status, detail="屏幕捕获暂不可用：" + str(exc)[:200]) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="屏幕捕获失败：" + str(exc)[:200]) from exc
        if (generation != service.generation or not service.config.enabled
                or lease_generation != service.lease_generation("dsh", activation_id)):
            raise HTTPException(status_code=409, detail="捕获期间屏幕图像设置已变化，本次图片已作废。")
        if session.activation_id != activation_id:
            raise HTTPException(status_code=409, detail="捕获期间 DSH 激活已结束，本次图片已作废。")
        response = JSONResponse(
            {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted", "capture": packet},
            headers={"Cache-Control": "no-store"},
        )
        service.consume("dsh", activation_id, packet["capture_id"])
        return response

    @app.post("/api/internal/dsh-bridge/input.early-silence")
    async def dsh_early_silence(request: Request) -> JSONResponse:
        payload, session = await dsh_callback_body(request)
        async with runtime._session_lock:
            async def schedule() -> str:
                controller = runtime.controller
                if controller is None:
                    return "unavailable"
                return await controller.request_early_silence()

            code, status = await session.accept_early_silence(payload, schedule)
        return JSONResponse(
            {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": code, "status": status},
            status_code=200 if code in {"accepted", "duplicate"} else 409,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/internal/dsh-bridge/shadow.activity")
    @app.post("/api/internal/dsh-bridge/shadow.lifecycle")
    async def dsh_shadow_activity(request: Request) -> JSONResponse:
        payload, session = await dsh_callback_body(request)
        code = await session.accept_activity(payload)
        if isinstance(payload.get("telemetry"), dict):
            await runtime.publish_shadow_product()
        return JSONResponse(
            {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": code},
            status_code=200 if code in {"accepted", "duplicate"} else 409,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/internal/dsh-bridge/shadow.context.read")
    async def dsh_shadow_context_read(request: Request) -> JSONResponse:
        payload, session = await dsh_callback_body(request)
        since = payload.get("since_revision")
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            raise HTTPException(status_code=422, detail="invalid since_revision")
        value = session.latest_context(since)
        return JSONResponse(
            {"protocol_version": BRIDGE_PROTOCOL_VERSION, "result": "accepted", **value},
            headers={"Cache-Control": "no-store"},
        )

    def authorize_dsh_plugin(request: Request) -> None:
        remote = request.client.host if request.client is not None else None
        if not runtime.dsh.bridge_hub.authorize(request.headers.get("authorization"), remote):
            raise HTTPException(status_code=401, detail="unauthorized")

    async def dsh_task_api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await runtime.dsh.bridge.task_api(method, path, payload)
        except DshBridgeError as exc:
            status = int(getattr(exc, "status_code", 503))
            raise HTTPException(status_code=status if status in {404, 409, 422} else 503,
                                detail=str(exc)) from exc

    @app.post("/api/internal/dsh-bridge/voice.publish")
    async def dsh_publish_voice_event(request: Request) -> JSONResponse:
        authorize_dsh_plugin(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="event must be an object")
        code = await runtime.publish_voice_event(payload)
        return JSONResponse(
            {"result": code},
            status_code=200 if code in {"accepted", "duplicate"} else 409,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/internal/dsh-bridge/mainline.deliver")
    async def dsh_deliver_mainline(request: Request) -> dict[str, Any]:
        authorize_dsh_plugin(request)
        payload = await request.json()
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str) or not payload[key].strip()
            for key in ("task_id", "occurrence_id", "text")
        ):
            raise HTTPException(status_code=422, detail="invalid scheduled task")
        async with runtime._session_lock:
            prior = runtime._task_deliveries.get(payload["occurrence_id"])
            if prior is not None:
                return {**prior, "result": "duplicate"}
            if runtime._handoff_marker.exists():
                try:
                    await runtime.reset_session(_lock_held=True, _strict_shadow=True)
                    runtime._handoff_marker.unlink(missing_ok=True)
                except Exception as exc:
                    raise HTTPException(status_code=503, detail="会话交接尚未完成。") from exc
            session = runtime.shadow_session
            if not isinstance(session, DshShadowAgentSession):
                raise HTTPException(status_code=503, detail="DSH 主线尚不可用。")
            runtime.continuity.user_started()
            runtime._dsh_busy = True
            try:
                async with session._wake_lock:
                    await session._ensure_open()
                    await session._sync_context(hold=session._gate_enabled and session.activation_id is not None)
                response = await runtime.dsh.bridge.call("task.deliver", {
                    "request_id": str(payload["occurrence_id"]),
                    "voice_session_id": session.voice_session_id,
                    "dsh_session_id": session.dsh_session_id,
                    "epoch": session.epoch,
                    "task_id": payload["task_id"],
                    "occurrence_id": payload["occurrence_id"],
                    "text": payload["text"],
                    "target": payload.get("target", "shadow_agent"),
                })
                if response.get("result") == "duplicate" and isinstance(response.get("original"), dict):
                    response = {**response["original"], "result": "duplicate"}
            except Exception:
                runtime._dsh_busy = False
                if runtime._voice_silence_anchor is not None:
                    runtime.continuity.arm(time.time(), runtime._continuity_snapshot)
                raise
            if response.get("result") not in {"accepted", "duplicate"}:
                raise HTTPException(status_code=503, detail="DSH task delivery failed")
            runtime._record_task_delivery(payload["occurrence_id"], response)
            return response

    @app.post("/api/internal/dsh-bridge/mainline.activity")
    async def dsh_mainline_activity(request: Request) -> dict[str, str]:
        authorize_dsh_plugin(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="invalid activity")
        return {"result": await runtime.note_dsh_activity(payload)}

    @app.post("/internal/application/wake-tasks")
    async def create_manual_wake_task(request: Request) -> dict[str, Any]:
        """手动创建入口：宿主身份校验后固定目标及空参考，不接受浏览器伪造来源。"""
        authorize_application(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求必须为有效 JSON（结构化数据）。") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="任务必须是对象。")
        rules = [key for key in ("at", "daily", "weekly", "every_seconds") if key in body]
        if len(rules) != 1:
            raise HTTPException(status_code=422, detail="必须且只能提供一种单次、每日、每周或间隔规则。")
        rule = rules[0]
        allowed = {"text", rule}
        if rule == "every_seconds":
            allowed.update({"loop", "trigger_count"})
        if set(body) - allowed or not {"text", rule}.issubset(body):
            raise HTTPException(status_code=422, detail="手动任务只允许任务说明、一个时间规则及间隔任务的循环与次数字段。")
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise HTTPException(status_code=422, detail="任务说明须为 1 至 500 字。")
        if rule == "every_seconds":
            seconds = body.get("every_seconds")
            loop = body.get("loop", True)
            count = body.get("trigger_count")
            if type(seconds) is not int or seconds < 60 or seconds > 86_400 or seconds % 60:
                raise HTTPException(status_code=422, detail="every_seconds（间隔秒数）须为 60 至 86400 秒之间的整分钟数。")
            if type(loop) is not bool:
                raise HTTPException(status_code=422, detail="loop（是否循环）必须是布尔值。")
            if loop and "trigger_count" in body:
                raise HTTPException(status_code=422, detail="循环任务不能同时设置 trigger_count（总触发次数）。")
            if not loop and (type(count) is not int or count < 1 or count > 9_007_199_254_740_991):
                raise HTTPException(status_code=422, detail="关闭循环时 trigger_count（总触发次数）必须为正整数。")
            if "trigger_count" in body and type(count) is not int:
                raise HTTPException(status_code=422, detail="trigger_count（总触发次数）必须为整数。")
        return await dsh_task_api("POST", "tasks", {
            "text": text.strip(), rule: body[rule],
            **({key: body[key] for key in ("loop", "trigger_count") if key in body} if rule == "every_seconds" else {}),
            "target": "shadow_agent", "reference": "", "creation_source": "manual",
        })

    @app.post("/internal/application/dsh-approval")
    async def decide_dsh_approval(request: Request) -> dict[str, Any]:
        """权限决定仅经本次桌面宿主认证转发，内部凭据不传到页面。"""
        authorize_application(request)
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"approval_id", "decision", "run_seq"}
                or not isinstance(body.get("approval_id"), str) or not 1 <= len(body["approval_id"]) <= 160
                or body["approval_id"] != body["approval_id"].strip() or "\0" in body["approval_id"]
                or not isinstance(body.get("decision"), str)
                or body["decision"] not in {"allowed-once", "allowed-until-silence", "rejected"}
                or type(body.get("run_seq")) is not int
                or not 0 <= body["run_seq"] <= (1 << 53) - 1):
            raise HTTPException(status_code=422, detail="权限决定需要有效的请求标识、运行序号以及同意或拒绝。")
        async with runtime._session_lock:
            session = runtime.shadow_session
            if not isinstance(session, DshShadowAgentSession):
                raise HTTPException(status_code=409, detail="当前没有 DSH（代理执行宿主）权限请求。")
            try:
                result = await session.decide_approval(body["approval_id"], body["decision"], body["run_seq"])
            except DshBridgeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.get("result") not in {"accepted", "duplicate"}:
            raise HTTPException(status_code=409, detail="权限请求已失效或已处理，请等待状态刷新。")
        return result

    @app.post("/api/internal/wake-tasks")
    async def create_wake_task_from_dsh(request: Request) -> dict[str, Any]:
        authorize_dsh_plugin(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="wake task must be an object")
        return await dsh_task_api("POST", "tasks", payload)

    @app.post("/api/internal/wake-tasks/{task_id}/cancel")
    async def cancel_wake_task_from_dsh(task_id: str, request: Request) -> dict[str, Any]:
        authorize_dsh_plugin(request)
        return await dsh_task_api("DELETE", f"tasks/{quote(task_id, safe='')}")

    @app.get("/api/internal/wake-tasks/{task_id}/reference")
    async def read_wake_reference_from_dsh(task_id: str, request: Request) -> dict[str, Any]:
        authorize_dsh_plugin(request)
        return await dsh_task_api("GET", f"tasks/{quote(task_id, safe='')}/reference")

    @app.get("/api/v1/wake-tasks")
    async def list_wake_tasks() -> dict[str, Any]:
        return await dsh_task_api("GET", "tasks")

    @app.patch("/api/v1/wake-tasks/{task_id}")
    async def update_wake_task(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await dsh_task_api("PATCH", f"tasks/{quote(task_id, safe='')}", body)

    @app.delete("/api/v1/wake-tasks/{task_id}")
    async def delete_wake_task(task_id: str) -> dict[str, bool]:
        result = await dsh_task_api("DELETE", f"tasks/{quote(task_id, safe='')}/archive")
        if result.get("deleted") is not True:
            raise HTTPException(status_code=409, detail="插件未确认归档删除，请刷新后重试。")
        return {"deleted": True}

    @app.post("/api/v1/wake-tasks/{task_id}/cancel")
    async def cancel_wake_task(task_id: str) -> dict[str, Any]:
        return await dsh_task_api("DELETE", f"tasks/{quote(task_id, safe='')}")

    def product_store_or_404() -> ProductSettingsStore:
        if product_settings is None:
            raise HTTPException(status_code=404, detail="产品设置服务尚未启用。")
        return product_settings

    @app.get("/api/v1/product/settings/hotwords")
    async def get_hotwords() -> dict:
        from ..asr.hotwords import DEFAULT_HOTWORDS
        store = product_store_or_404()
        return {"settings": store.settings_only().get("hotwords", DEFAULT_HOTWORDS),
                "status": runtime.hotwords.last_status}

    @app.put("/api/v1/product/settings/audio-enhancements")
    async def save_audio_enhancements(body: dict[str, Any]) -> dict:
        product_store_or_404()
        try:
            value = await runtime.switch_audio_enhancements(body)
            return {"settings": value, "pending": False, "message": "音频增强设置已生效。"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"音频增强切换失败：{exc}") from exc

    @app.put("/api/v1/product/settings/hotwords")
    async def save_hotwords(body: dict[str, Any]) -> dict:
        service = runtime.hotwords
        try:
            async with runtime_operation(runtime, "hotwords", "热词索引"), service.update_lock:
                bank = await asyncio.to_thread(service.prepare_measured, body)
                value = await asyncio.to_thread(product_store_or_404().save_hotwords, bank.settings)
                service.bank = bank  # 持久化成功后原子换入；已有轮次仍用原词表。
            return {"settings": value, "effective_from": "next_turn", "pending": False,
                    "message": "热词已保存，从下一轮发言生效，无需重启。"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/product/hotwords/import")
    async def import_hotwords(body: dict[str, Any]) -> dict:
        from ..asr.hotwords import parse_hotword_text
        product_store_or_404()
        try:
            return {"entries": parse_hotword_text(body.get("text"))}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/product/hotwords/preview")
    async def preview_hotwords(body: dict[str, Any]) -> dict:
        product_store_or_404()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise HTTPException(status_code=422, detail="试测文本须为 1～500 个字符。")
        from ..asr.hotwords import HotwordService
        service = HotwordService()
        try:
            await service.configure(body.get("settings"))
            return await service.correct(text, "preview", lane="final")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            service.close()

    @app.get("/api/v1/product/compute-status")
    async def get_product_compute_status(refresh: bool = False) -> dict[str, Any]:
        return await runtime.compute_status(refresh_capabilities=True) if refresh else await runtime.compute_status()

    @app.put("/api/v1/product/settings/compute")
    async def update_product_compute(body: dict[str, Any]) -> dict[str, Any]:
        try:
            # 未开放热切换的设备项沿用旧契约：保存单项偏好，重启应用后生效。
            model_id = body.get("model_id") if isinstance(body, dict) else None
            if (set(body) == {"model_id", "device"} and isinstance(model_id, str)
                    and model_id in {"voice_vad", "kws", "smart_turn"}):
                store = product_store_or_404()
                device = body.get("device")
                if device == "cuda":
                    from . import websocket as session_module
                    from ..compute import model_hardware_status
                    hardware = await asyncio.to_thread(session_module.hardware_status, refresh=True)
                    hardware = await asyncio.to_thread(model_hardware_status, hardware, runtime.config, verify=True)
                    capability = hardware.get("models", {}).get(model_id, {})
                    if not capability.get("gpu_available"):
                        reason = capability.get("reason") or "GPU 后端不可用。"
                        raise ValueError(f"{model_id} 暂不能使用 GPU：{reason}")
                value = await asyncio.to_thread(store.save_compute_model, model_id, device)
                return {
                    "settings": {"compute": value},
                    "effective_from": "application_restart",  # effective_from（生效时机）：重启应用后生效。
                    "restart_required": True,  # restart_required（是否需要重启）：是。
                    "pending": True,  # pending（待生效标记）：设置尚未作用于当前运行实例。
                    "message": "该模型暂不支持热切换；设备设置已保存，重新启动应用后生效。",
                }
            if not isinstance(body, dict) or set(body) != {"model_id", "device"}:
                raise ValueError("计算设备请求必须只包含 model_id（目标模型编号）和 device（目标设备模式）。")
            model_id, device = body.get("model_id"), body.get("device")
            if not isinstance(model_id, str) or model_id not in {"paraformer", "emotion", "campplus"}:
                raise ValueError("model_id（目标模型编号）只支持 paraformer、emotion 和 campplus。")
            if not isinstance(device, str) or device not in {"auto", "cpu", "cuda"}:
                raise ValueError("device（目标设备模式）必须是 auto、cpu 或 cuda。")

            # 切换耗时主要在模型进程重建和推理预热；HTTP（网页接口）只负责接纳，
            # 实际阶段由同一运行操作快照持续发布给状态卡，避免“保存中”占住按钮十余秒。
            label = {"paraformer": "Paraformer", "emotion": "emotion2vec", "campplus": "CAM++"}[model_id]
            operations = runtime_operations(runtime)
            ticket = operations.reserve(f"compute_device:{model_id}", f"{label}运行设备切换",
                                        phase="switching", pause=True)
            tasks = getattr(runtime, "_compute_switch_tasks", None)
            if tasks is None:
                tasks = runtime._compute_switch_tasks = {}

            async def run_compute_switch():
                try:
                    await runtime.switch_compute_device(body, ticket=ticket)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("计算设备切换后台操作失败：model_id=%s", model_id)

            task = asyncio.create_task(run_compute_switch(), name=f"compute-switch-{model_id}")
            tasks[task] = ticket

            def release_unstarted(done):
                tasks.pop(done, None)
                # If shutdown cancels the task before its coroutine enters
                # RuntimeOperations.run, release the synchronous reservation.
                if ticket.started or ticket.released:
                    return
                failure = asyncio.CancelledError() if done.cancelled() else done.exception()
                asyncio.create_task(operations.finish(ticket, failure))

            task.add_done_callback(release_unstarted)
            await operations.publish()
            return {
                "effective_from": "immediate",  # effective_from（生效时机）：即时生效。
                "restart_required": False,  # restart_required（是否需要重启）：否。
                "accepted": True,  # accepted（已接纳标记）：操作已交给后台，最终状态由运行状态卡发布。
                "operation_id": ticket.key,  # operation_id（运行操作编号）：用于关联本次状态卡进度。
                "pending": True,  # pending（待生效标记）：切换完成并核验前仍待生效。
                "message": "设备切换已开始，进度与结果会显示在运行状态中。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if isinstance(exc, (RuntimeError, OSError)):
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.get("/api/v1/tts/status")
    async def get_tts_status() -> dict[str, Any]:
        client = getattr(runtime.tts, "local", None) if runtime.tts is not None else None
        server = getattr(client, "server", None)
        refresh_status = getattr(server, "refresh_status", None)
        if callable(refresh_status):
            await refresh_status()
        return {
            **runtime.local_tts_status_payload(),
            "worker": {"state": "stopped"},  # worker（旧式本地工作进程）兼容字段。
        }

    @app.patch("/api/v1/tts/aniflive/models/{model_id}")
    async def rename_aniflive_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """model_id（稳定模型标识）；body（只含 name（模型显示名称））。"""
        if set(body) != {"name"}:
            raise HTTPException(status_code=422, detail="模型重命名只接受 name（显示名称）。")
        try:
            from ..tts.aniflive_models import AnifLiveModelRegistry
            registry = AnifLiveModelRegistry(runtime._runtime_local_root, runtime._tts_voice_root.parent)
            return await asyncio.to_thread(registry.rename, model_id, body.get("name"))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def tts_voice_library_or_503(model=None):
        libraries = getattr(runtime, "voice_library", None)
        if libraries is None:
            raise HTTPException(status_code=503, detail="音色库不可用。")
        try:
            target = runtime.config.tts.local_model if model == 'aniflive' else model
            return libraries.for_model(target) if target else libraries
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def validate_remote_tts_import_binding(payload: dict[str, Any], public_fields: set[str]):
        internal_fields = {"_authenticated_device_id", "_connection_generation", "_executor_generation"}
        if set(payload) != public_fields | internal_fields:
            raise HTTPException(status_code=422, detail="TTS 文件导入票据字段无效。")
        device_id = payload.get("_authenticated_device_id")
        connection_generation = payload.get("_connection_generation")
        executor_generation = payload.get("_executor_generation")
        parent_request_id = payload.get("parent_request_id")
        if (not isinstance(device_id, str) or not valid_device_request_id(device_id)
                or type(connection_generation) is not int or connection_generation < 1
                or type(executor_generation) is not int or executor_generation < 1
                or not valid_device_request_id(parent_request_id)):
            raise HTTPException(status_code=422, detail="TTS 文件导入请求身份或代次无效。")
        connection = runtime.device_registry.connection(device_id)
        if (connection is None or not connection.connected or "file_transfer" not in connection.capabilities
                or connection.connection_generation != connection_generation
                or connection.executor_generation != executor_generation):
            raise HTTPException(status_code=409, detail="TTS 文件导入设备连接或执行器代次已变化。")
        return connection

    @app.post("/internal/application/tts-assets/import/prepare")
    async def prepare_remote_tts_asset_import(request: Request) -> JSONResponse:
        """prepare_remote_tts_asset_import（预留 TTS 音频票据）：只创建 DFT 文件传输票据。"""
        authorize_application(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="TTS 文件导入准备请求不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="TTS 文件导入准备请求必须是 JSON 对象。")
        public_fields = {"model_id", "purpose", "filename", "expected_bytes", "parent_request_id"}
        connection = validate_remote_tts_import_binding(payload, public_fields)
        library = tts_voice_library_or_503(payload.get("model_id"))
        model_id = getattr(library, "model_id", None) or getattr(library, "model", None)
        store = getattr(runtime, "device_file_transfer_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
        transfer_id = device_file_transfer_id(connection, payload["parent_request_id"])
        try:
            ticket = await asyncio.to_thread(store.prepare_tts_asset_import,
                transfer_id=transfer_id, device_id=connection.device_id,
                connection_generation=connection.connection_generation,
                executor_generation=connection.executor_generation,
                parent_request_id=payload["parent_request_id"], model_id=model_id,
                purpose=payload["purpose"], filename=payload["filename"],
                expected_bytes=payload["expected_bytes"])
        except Exception as exc:
            raise HTTPException(status_code=409, detail="TTS 文件导入请求未能绑定到唯一 DFT 票据。") from exc
        return JSONResponse(ticket, headers={"Cache-Control": "no-store"})

    @app.post("/internal/application/tts-assets/audio/read")
    async def read_remote_tts_asset_audio(request: Request) -> JSONResponse:
        """read_remote_tts_asset_audio（远程音频素材读取）：只回传已登记且不超过 1 MiB 的音频字节。"""
        authorize_application(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="TTS 音频读取请求不是有效 JSON。") from exc
        public_fields = {"model_id", "asset_id"}
        internal_fields = {"_authenticated_device_id", "_connection_generation", "_executor_generation"}
        if not isinstance(payload, dict) or set(payload) != public_fields | internal_fields:
            raise HTTPException(status_code=422, detail="TTS 音频读取字段不符合固定契约。")
        device_id = payload.get("_authenticated_device_id")
        connection_generation = payload.get("_connection_generation")
        executor_generation = payload.get("_executor_generation")
        if (not valid_device_request_id(device_id) or type(connection_generation) is not int
                or connection_generation < 1 or type(executor_generation) is not int
                or executor_generation < 1):
            raise HTTPException(status_code=422, detail="TTS 音频读取设备身份或代次无效。")
        connection = runtime.device_registry.connection(device_id)
        if (connection is None or not connection.connected or "file_transfer" not in connection.capabilities
                or connection.connection_generation != connection_generation
                or connection.executor_generation != executor_generation):
            raise HTTPException(status_code=409, detail="TTS 音频读取设备连接或执行器代次已变化。")
        model_id, asset_id = payload.get("model_id"), payload.get("asset_id")
        if (not isinstance(model_id, str) or not model_id or len(model_id) > 128
                or not model_id.isascii()
                or any(not (character.isalnum() or character in "._:-") for character in model_id)
                or not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_id)):
            raise HTTPException(status_code=422, detail="TTS 音色库或素材编号无效。")
        try:
            library = tts_voice_library_or_503(model_id)
            with library._lock:
                metadata = library._index["assets"].get(asset_id)
            if not isinstance(metadata, dict):
                raise HTTPException(status_code=404, detail="TTS 音频素材不存在。")
            asset_root = Path(library.assets_dir).resolve()
            maximum_audio_bytes = 1024 * 1024  # maximum_audio_bytes（远程预览最大原始音频长度：1 MiB）。

            def checked_audio_path(path: Path):
                if path.is_symlink() or not path.is_file():
                    raise HTTPException(status_code=404, detail="TTS 音频素材不存在或不可读取。")
                resolved_path = path.resolve(strict=True)
                if not resolved_path.is_relative_to(asset_root):
                    raise HTTPException(status_code=404, detail="TTS 音频素材不存在或不可读取。")
                return path.stat()

            high_fidelity_path = library.high_fidelity_path(asset_id)
            use_high_fidelity = high_fidelity_path is not None
            if use_high_fidelity:
                data_path = high_fidelity_path
                media_type = "audio/wav"
                before = checked_audio_path(data_path)
                if before.st_size > maximum_audio_bytes:
                    # 高保真旁路副本过大时，改用登记的规范素材，仍对实际回传字节实施相同大小限制。
                    data_path = (library._source_path(asset_id) if metadata.get("format") == "source"
                                 else library.asset_path(asset_id))
                    media_type = library.asset_media_type(asset_id)
                    use_high_fidelity = False
                    before = checked_audio_path(data_path)
            else:
                data_path = (library._source_path(asset_id) if metadata.get("format") == "source"
                             else library.asset_path(asset_id))
                media_type = library.asset_media_type(asset_id)
                before = checked_audio_path(data_path)
            if before.st_size <= 0:
                raise HTTPException(status_code=404, detail="TTS 音频素材为空。")
            if before.st_size > maximum_audio_bytes:
                raise HTTPException(status_code=413, detail="TTS 音频素材超过 1 MiB 预览上限。")
            data = await asyncio.to_thread(data_path.read_bytes)
            after = data_path.stat()
            if (data_path.is_symlink() or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or before.st_ino != after.st_ino
                    or len(data) != after.st_size):
                raise HTTPException(status_code=409, detail="TTS 音频素材读取期间发生变化。")
            if len(data) > maximum_audio_bytes:
                raise HTTPException(status_code=413, detail="TTS 音频素材超过 1 MiB 预览上限。")
            remote_import = metadata.get("remote_import")
            expected_sha256 = None
            if isinstance(remote_import, dict):
                stored_digest_field = ("high_fidelity_sha256" if use_high_fidelity else "stored_sha256")
                expected_sha256 = remote_import.get(stored_digest_field)
                if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                    raise HTTPException(status_code=409, detail="TTS 音频素材缺少有效的导入摘要。")
            elif use_high_fidelity:
                expected_sha256 = metadata.get("high_fidelity_sha256")
                if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
                    raise HTTPException(status_code=409, detail="TTS 高保真素材登记摘要无效。")
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise HTTPException(status_code=409, detail="TTS 音频素材与导入时登记的 SHA-256 摘要不匹配。")
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="TTS 音频素材不存在或文件缺失。") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="TTS 音频素材无法安全读取。") from exc
        return JSONResponse({"data_base64": base64.b64encode(data).decode("ascii"),
            "media_type": media_type, "bytes": len(data), "sha256": actual_sha256,
            "fields": {"data_base64": "已登记 TTS 音频的规范 Base64 字节内容",
                "media_type": "原音频或高保真旁路副本的媒体类型",
                "bytes": "实际返回音频内容的字节数，最大 1 MiB",
                "sha256": "本次实际回传音频内容的 SHA-256 摘要"}},
            headers={"Cache-Control": "no-store"})

    @app.post("/internal/application/tts-assets/import/commit")
    async def commit_remote_tts_asset_import(request: Request) -> JSONResponse:
        """commit_remote_tts_asset_import（提交 TTS 音频导入）：复核 DFT staging 后幂等写 VoiceLibrary。"""
        authorize_application(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="TTS 文件导入提交请求不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="TTS 文件导入提交请求必须是 JSON 对象。")
        public_fields = {"model_id", "purpose", "filename", "expected_bytes", "expected_sha256",
                         "parent_request_id", "transfer_id"}
        connection = validate_remote_tts_import_binding(payload, public_fields)
        model_library = tts_voice_library_or_503(payload.get("model_id"))
        model_id = getattr(model_library, "model_id", None) or getattr(model_library, "model", None)
        transfer_id = device_file_transfer_id(connection, payload["parent_request_id"])
        if payload.get("transfer_id") != transfer_id:
            raise HTTPException(status_code=409, detail="TTS transfer_id 与设备、代次或 parent_request_id 不匹配。")
        store = getattr(runtime, "device_file_transfer_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
        binding = {
            "transfer_id": transfer_id,
            "device_id": connection.device_id,
            "connection_generation": connection.connection_generation,
            "executor_generation": connection.executor_generation,
            "parent_request_id": payload["parent_request_id"],
            "model_id": model_id,
            "purpose": payload["purpose"],
            "filename": payload["filename"],
            "expected_bytes": payload["expected_bytes"],
            "expected_sha256": payload["expected_sha256"],
        }

        async def cleanup_committed_staging() -> None:
            """只在 VoiceLibrary（音色库）确认提交后，尽力关闭精确绑定的 DFT 暂存。"""
            try:
                await asyncio.to_thread(store.close_tts_asset_import, **binding)
            except Exception:
                logger.warning("已提交 TTS 素材的 DFT 暂存清理失败 transfer_id=%s", transfer_id,
                               exc_info=True)

        try:
            previous = await asyncio.to_thread(model_library.get_remote_import_result, binding)
            if previous is not None:
                await cleanup_committed_staging()
                return JSONResponse(previous, headers={"Cache-Control": "no-store"})
            data, verified = await asyncio.to_thread(store.read_tts_asset_import,
                transfer_id=transfer_id, device_id=connection.device_id,
                connection_generation=connection.connection_generation,
                executor_generation=connection.executor_generation,
                parent_request_id=payload["parent_request_id"], model_id=model_id,
                purpose=payload["purpose"], filename=payload["filename"],
                expected_bytes=payload["expected_bytes"], expected_sha256=payload["expected_sha256"])
            if verified != binding:
                raise HTTPException(status_code=409, detail="TTS DFT 票据绑定与提交请求不一致。")
            result = await asyncio.to_thread(model_library.commit_remote_import, data, **binding)
            try:
                await asyncio.to_thread(store.mark_tts_asset_import_committed,
                    transfer_id=transfer_id, device_id=connection.device_id,
                    connection_generation=connection.connection_generation,
                    executor_generation=connection.executor_generation, metadata={
                        "transfer_id":transfer_id,"device_id":connection.device_id,
                        "connection_generation":connection.connection_generation,
                        "executor_generation":connection.executor_generation,
                        "parent_request_id":payload["parent_request_id"],"model_id":model_id,
                        "purpose":payload["purpose"],"filename":payload["filename"],
                        "expected_bytes":payload["expected_bytes"]})
            except Exception:
                # VoiceLibrary（音色库）已经持久化并返回结果；标记和暂存回收均属于尽力清理。
                logger.warning("已提交 TTS 素材的 DFT 完成标记失败 transfer_id=%s", transfer_id,
                               exc_info=True)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail="TTS 文件票据未完成、内容摘要错误或已绑定其他导入。") from exc
        await cleanup_committed_staging()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/internal/application/tts-assets/import/abort")
    async def abort_remote_tts_asset_import(request: Request) -> JSONResponse:
        """abort_remote_tts_asset_import（取消 TTS 上传）：只清理未提交 DFT 暂存。"""
        authorize_application(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="TTS 文件导入取消请求不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="TTS 文件导入取消请求必须是 JSON 对象。")
        public_fields = {"parent_request_id", "transfer_id"}
        connection = validate_remote_tts_import_binding(payload, public_fields)
        expected_transfer_id = device_file_transfer_id(connection, payload["parent_request_id"])
        if payload.get("transfer_id") != expected_transfer_id:
            raise HTTPException(status_code=409, detail="TTS abort transfer_id 与原设备请求不匹配。")
        store = getattr(runtime, "device_file_transfer_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="设备文件暂存目录尚未就绪。")
        try:
            aborted = await asyncio.to_thread(store.abort_tts_asset_import,
                transfer_id=expected_transfer_id, device_id=connection.device_id,
                connection_generation=connection.connection_generation,
                executor_generation=connection.executor_generation,
                parent_request_id=payload["parent_request_id"])
        except Exception as exc:
            raise HTTPException(status_code=409, detail="TTS abort 请求与原 DFT 票据不匹配。") from exc
        return JSONResponse({"transfer_id":expected_transfer_id,"parent_request_id":payload["parent_request_id"],
            "aborted":aborted,"committed":not aborted,
            "fields":{"transfer_id":"原 TTS DFT 票据编号","parent_request_id":"原稳定导入请求编号",
                "aborted":"是否已清理未提交暂存","committed":"是否已存在已提交的 TTS 素材"}},
            headers={"Cache-Control":"no-store"})

    def tts_voice_operations_or_503():
        operations = getattr(runtime, "tts_voice_operations", None)
        if operations is None:
            raise HTTPException(status_code=503, detail="音色操作服务未就绪。")
        return operations

    @app.post("/api/v1/tts/aniflive/import/scan")
    async def scan_aniflive_import(body: dict[str, Any]):
        try:
            return await asyncio.to_thread(
                aniflive_import_or_503().scan, body.get("source_type"), body.get("source"),
            )
        except (ValueError, OSError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/tts/aniflive/import/jobs")
    async def start_aniflive_import(body: dict[str, Any]):
        if runtime.application_initializing:
            raise HTTPException(status_code=409, detail="应用正在初始化，请稍后导入。")
        try:
            return JSONResponse(await asyncio.to_thread(aniflive_import_or_503().start, body), status_code=202)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="扫描记录已过期，请重新扫描模型来源。") from exc
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/aniflive/import/jobs/{task_id}")
    async def get_aniflive_import(task_id: str):
        try:
            return await asyncio.to_thread(aniflive_import_or_503().task, task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="AnifLive 导入任务不存在或已清理。") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/aniflive/import/pending")
    async def get_pending_aniflive_imports():
        try:
            return {"jobs": await asyncio.to_thread(aniflive_import_or_503().pending)}
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/tts/aniflive/import/jobs/{task_id}/reference")
    async def add_aniflive_import_reference(task_id: str, body: dict[str, Any]):
        try:
            reference_path = None
            asset_id = body.get("reference_asset_id")
            if asset_id is not None:
                library = tts_voice_library_or_503("aniflive")
                reference_path = library.high_fidelity_path(asset_id) or library.asset_path(asset_id)
            service = aniflive_import_or_503()
            result = await asyncio.to_thread(
                service.add_reference, task_id,
                reference_path=reference_path,
                reference_text=body.get("reference_text", ""),
                reference_language=body.get("reference_language", "zh"),
            )
            return service._public_task(result)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="导入任务或参考音频不存在。") from exc
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post('/api/v1/tts/aniflive/models/{model_id}/references/import')
    async def start_aniflive_reference_import(model_id: str, body: dict[str, Any]):
        try:
            return JSONResponse(await asyncio.to_thread(
                aniflive_import_or_503().start_existing_references, model_id, body), status_code=202)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def start_tts_voice_operation(callback):
        if runtime.application_initializing:
            raise HTTPException(status_code=409, detail="应用正在初始化，请稍后操作。")
        async with runtime._session_lock:
            try:
                return JSONResponse(callback(), status_code=202)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/voices/{model}")
    async def get_tts_voices(model: str):
        library = tts_voice_library_or_503(model)
        if not library._index.get('default_voice'):
            try:
                from ..tts.aniflive_models import AnifLiveModelRegistry
                from ..tts.aniflive_reference_registration import register_model_default
                target = runtime.config.tts.local_model if model == 'aniflive' else model
                package, _, _ = await asyncio.to_thread(
                    AnifLiveModelRegistry(runtime._runtime_local_root, runtime._tts_voice_root.parent).resolve, target)
                await asyncio.to_thread(register_model_default, library, package)
            except (OSError, ValueError, KeyError) as exc:
                from ..tts.voices import safe_voice_diagnostic
                raise HTTPException(status_code=409, detail='模型默认参考无法登记：' + safe_voice_diagnostic(exc)) from exc
        return {"voices": library.list_voices({model: library.active_selection}),
                "active_voice": library.active_selection, "model": model}

    @app.get("/api/v1/tts/voices/{model}/capabilities")
    async def get_tts_voice_capabilities(model: str):
        """只返回实际生效且完成准备的音色能力。"""
        from ..tts.controls import profile_for
        library = tts_voice_library_or_503(model)
        client = getattr(runtime.tts, 'local', None)
        actual_model = getattr(client, 'model_id', None)
        target = runtime.config.tts.local_model if model == 'aniflive' else model
        active = library.active_selection
        if client is None or actual_model != target or runtime.config.tts.backend != 'aniflive':
            return {'model': target, 'active_voice': active, 'prepared': False, 'capabilities': [], 'expressions': [], 'digest': None}
        profile = profile_for(client, backend='aniflive', enabled=getattr(runtime.tts, 'speech_control_enabled', True))
        return {'model': target, 'active_voice': active, 'prepared': bool(getattr(client, '_prepared_expression_snapshot', None)),
                'capabilities': list(profile.names),
                'expressions': [{'tag': tag, 'name': name, 'intensity_levels': list(levels)} for tag, name, levels in profile.expressions],
                'digest': profile.identity}

    @app.post("/api/v1/tts/minimax/voice-name")
    async def get_minimax_voice_name(body: dict[str, Any]):
        if set(body) != {"voice_id"}:
            raise HTTPException(status_code=422, detail="音色查询仅接受 voice_id（音色标识）。")
        voice_id = body.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise HTTPException(status_code=422, detail="voice_id（音色标识）不能为空。")
        voice_id = voice_id.strip()
        payload = await _minimax_voice_catalog(runtime)
        found, voice_name = _minimax_voice_name(payload, voice_id)
        return {"voice_id": voice_id, "voice_name": voice_name if found else None}

    @app.post("/api/v1/tts/voices/{model}")
    async def create_tts_voice(model: str, body: dict[str, Any]):
        try:
            validate_tts_candidate_steps(model, body)
            voice = await asyncio.to_thread(tts_voice_library_or_503(model).create_voice, body)
            return {"voice": voice}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put('/api/v1/tts/voices/{model}/{voice_id}/default-reference')
    async def set_tts_default_reference(model: str, voice_id: str, body: dict[str, Any]):
        if set(body) != {'reference_id', 'expected_revision'}:
            raise HTTPException(status_code=422, detail='仅接受 reference_id（参考编号，可为空）和 expected_revision（预期版本）。')
        async with runtime._session_lock:
            try:
                voice = await asyncio.to_thread(tts_voice_library_or_503(model).set_default_reference,
                    voice_id, body['expected_revision'], body['reference_id'])
                client = getattr(runtime.tts, 'local', None)
                if client:
                    client._warmed = False
                    client._prepared_expression_snapshot = None
                await runtime.bus.publish(runtime.snapshot())
                return {'voice': voice}
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/v1/tts/voices/{model}/selection")
    async def select_tts_voice(model: str, body: dict[str, Any]):
        if set(body) != {"selection"}:
            raise HTTPException(status_code=422, detail="仅接受 selection（音色选择），为空时回默认项。")
        async with runtime._session_lock:
            try:
                selected = tts_voice_library_or_503(model).activate(body["selection"])
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            client = getattr(runtime.tts, "local", None)
            if client:
                client._warmed = False
            await runtime.bus.publish(runtime.snapshot())
            return {"selection": selected, "effective_from": "next_answer"}

    @app.post("/api/v1/tts/voices/{model}/restore-default")
    async def restore_tts_default(model: str):
        try:
            voice = await asyncio.to_thread(tts_voice_library_or_503(model).restore_default)
            await runtime.bus.publish(runtime.snapshot())
            return {"voice": voice, "effective_from": "next_answer"}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/v1/tts/voices/{model}/{voice_id}")
    async def update_tts_voice(model: str, voice_id: str, body: dict[str, Any]):
        try:
            previous = tts_voice_library_or_503(model).get_voice(voice_id)
            validate_tts_candidate_steps(model, body, previous=previous)
            expected = body.pop("expected_revision", None)
            voice = await asyncio.to_thread(tts_voice_library_or_503(model).update_voice, voice_id, expected, body)
            client = getattr(runtime.tts, "local", None)
            if client:
                client._warmed = False
            await runtime.bus.publish(runtime.snapshot())
            return {"voice": voice, "effective_from": "next_answer"}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def validate_tts_candidate_steps(model, body, *, previous=None):
        """新样音绑定独立制作配方；旧存档允许改名、校对转写，正式步数不影响存档。"""
        from ..tts.voices import VoiceLibrary, DESIGN_INFERENCE_STEPS, DESIGN_GENERATION_RECIPE
        same_asset = previous is not None and body.get("reference_asset_id") == previous.get("reference_asset_id")
        if body.get("source") != "design":
            if not str(body.get("reference_text") or "").strip() and not (
                same_asset and not previous.get("reference_text")
            ):
                raise ValueError("请填写参考音频的准确转写后保存克隆音色。")
            return
        if (same_asset and previous.get("source") == "design"
                and VoiceLibrary.design_signature(body) == VoiceLibrary.design_signature(previous)):
            return
        if (body.get("inference_steps") != DESIGN_INFERENCE_STEPS
                or body.get("generation_recipe") != DESIGN_GENERATION_RECIPE):
            raise ValueError("请使用 30 步完整解码重新生成目标样音后保存。")

    @app.delete("/api/v1/tts/voices/{model}/{voice_id}")
    async def delete_tts_voice(model: str, voice_id: str):
        try:
            await asyncio.to_thread(tts_voice_library_or_503(model).delete_voice, voice_id)
            await runtime.bus.publish(runtime.snapshot())
            return {"deleted": True}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/tts/assets/{model}/import")
    async def import_tts_asset(model: str, request: Request, filename: str = ""):
        library = tts_voice_library_or_503(model)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="参考音频超过 20 MiB（二进制兆字节）。")
        try:
            asset = await asyncio.to_thread(library.put_import, bytes(data), filename)
            return {"asset": asset}
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post('/api/v1/tts/assets/{model}/source')
    async def import_tts_source(model: str, request: Request, filename: str = ''):
        """先保存原录音，超出工作参考时长时仍可播放和裁剪。"""
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail='来源录音超过 20 MiB（二进制兆字节）。')
        try:
            asset = await asyncio.to_thread(tts_voice_library_or_503(model).put_source, bytes(data), filename)
            return {'asset': asset}
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/assets/{model}/{asset_id}/audio")
    async def get_tts_asset_audio(model: str, asset_id: str):
        try:
            library = tts_voice_library_or_503(model)
            metadata = library._index['assets'].get(asset_id, {})
            if metadata.get('format') != 'source':
                library.asset_path(asset_id)  # 主素材缺失时不能把残留旁路副本当成完整素材。
            original = library.high_fidelity_path(asset_id)
            data = await asyncio.to_thread(original.read_bytes if original else library.asset_bytes, *(() if original else (asset_id,)))
            return Response(content=data, media_type='audio/wav' if original else library.asset_media_type(asset_id), headers={"Cache-Control": "no-store"})
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/v1/tts/assets/{model}/{asset_id}/clip')
    async def clip_tts_asset(model: str, asset_id: str, body: dict[str, Any]):
        if set(body) != {'start_ms', 'end_ms'}:
            raise HTTPException(status_code=422, detail='裁剪只接受起点与终点毫秒。')
        try:
            asset = await asyncio.to_thread(tts_voice_library_or_503(model).clip_source,
                                            asset_id, body['start_ms'], body['end_ms'])
            return {'asset': asset}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/tts/operations/preview")
    async def start_tts_preview(body: dict[str, Any]):
        manager = tts_voice_operations_or_503()
        return await start_tts_voice_operation(lambda: manager.start_preview(body.get("model"), body.get("voice"), body.get("text")))

    @app.post("/api/v1/tts/operations/minimax-preview")
    async def start_minimax_tts_preview(body: dict[str, Any]):
        if set(body) != {"online_model", "online_voice_id"}:
            raise HTTPException(status_code=422, detail="试听仅接受 online_model（在线模型）和 online_voice_id（在线音色标识）。")
        manager = tts_voice_operations_or_503()
        return await start_tts_voice_operation(
            lambda: manager.start_minimax_preview(body["online_model"], body["online_voice_id"]),
        )

    @app.post("/api/v1/tts/operations/play")
    async def start_tts_play(body: dict[str, Any]):
        return await start_tts_voice_operation(lambda: tts_voice_operations_or_503().start_play(body.get("model"), body.get("asset_id")))

    @app.post("/api/v1/tts/operations/transcribe")
    async def start_tts_transcribe(body: dict[str, Any]):
        # 独立精修请求允许附件先保存；忙碌失败仍保留附件供手填。
        try:
            payload = tts_voice_operations_or_503().start_transcribe(body.get("model"), body.get("asset_id"))
            return JSONResponse(payload, status_code=202)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/operations/{operation_id}")
    async def get_tts_operation(operation_id: str):
        try:
            return tts_voice_operations_or_503().get(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="音色操作不存在或已过期。") from exc

    @app.post("/api/v1/tts/operations/{operation_id}/cancel")
    async def cancel_tts_operation(operation_id: str):
        try:
            return await tts_voice_operations_or_503().cancel_confirmed(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="音色操作不存在或已过期。") from exc

    @app.put("/api/v1/product/settings/tts")
    async def update_product_tts(body: dict[str, Any]) -> dict[str, Any]:
        return await apply_product_tts(body)

    async def apply_product_tts(body: dict[str, Any], *, prepared: bool = False) -> dict[str, Any]:
        store = product_store_or_404()
        if not body or set(body) - {
            "backend", "local_model", "online_model", "online_voice_id",
            "local_optimization", "acoustic_continuation", "speech_control_enabled", "gpu_max_performance", "aniflive_chunk_optimization",
            "aniflive_first_audio_wait_ms", "local_model_revision",
        }:
            raise HTTPException(
                status_code=422,
                detail="TTS 设置包含不支持的字段。",
            )
        optimization = {field: body.get(field, getattr(runtime.config.tts, field))
                        for field in ("local_optimization", "acoustic_continuation", "speech_control_enabled")}
        if any(not isinstance(value, bool) for value in optimization.values()):
            raise HTTPException(status_code=422, detail="智能语音调节、本地优化与声学续接开关必须为布尔值。")
        from ..tts.settings import validate_options, valid_local_model
        try:
            optimization.update(validate_options({**{key: getattr(runtime.config.tts, key) for key in
                ("gpu_max_performance", "aniflive_chunk_optimization", "aniflive_first_audio_wait_ms", "local_model_revision")}, **body}))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        local_model = body.get("local_model", runtime.config.tts.local_model)
        backend = body.get("backend", runtime.config.tts.backend)
        if backend not in {"minimax", "aniflive"} or not valid_local_model(local_model):
            raise HTTPException(status_code=422, detail="backend（语音后端）或 local_model（登记模型编号）无效。")
        if local_model != runtime.config.tts.local_model and "local_model_revision" not in body:
            optimization["local_model_revision"] = None
        if backend == "aniflive":
            from ..tts.aniflive_models import AnifLiveModelRegistry
            try:
                _, revision, _ = AnifLiveModelRegistry(runtime._runtime_local_root, runtime._tts_voice_root.parent).resolve(
                    local_model, optimization["local_model_revision"])
                optimization["local_model_revision"] = revision
            except (ValueError, OSError, KeyError) as exc:
                raise HTTPException(status_code=422, detail="所选本地模型尚未完成组件配置及本机验证。") from exc
        selection_requested = bool(set(body) & {"backend", "local_model"})
        online_requested = bool(set(body) & {"online_model", "online_voice_id"})
        online_settings: dict[str, str] = {}
        online_config: dict[str, str] = {}
        if online_requested:
            saved_tts = store.settings_only().get("tts", {})
            online_model = body.get("online_model", saved_tts.get("online_model", runtime.config.tts.model))
            online_voice_id = body.get("online_voice_id", saved_tts.get("online_voice_id", runtime.config.tts.voice_id))
            if not isinstance(online_model, str) or online_model not in PRODUCT_MINIMAX_TTS_MODELS:
                raise HTTPException(status_code=422, detail="online_model（在线模型）必须为 speech-2.8-turbo 或 speech-2.8-hd。")
            if not isinstance(online_voice_id, str) or not online_voice_id.strip():
                raise HTTPException(status_code=422, detail="online_voice_id（在线音色标识）不能为空。")
            online_voice_id = online_voice_id.strip()
            if not prepared:
                await _validate_minimax_voice_id(runtime, online_voice_id)
            online_settings = {"online_model": online_model, "online_voice_id": online_voice_id}
            online_config = {"model": online_model, "voice_id": online_voice_id}
        if not selection_requested and not online_requested:
            async with runtime._session_lock:
                saved = store.save_tts_local_model(optimization)
                if getattr(runtime, "_pending_tts_update", None) is not None:
                    runtime._pending_tts_update.update(optimization)
                runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, **optimization))
                if runtime.tts is not None:
                    runtime.tts.speech_control_enabled = optimization["speech_control_enabled"]
                output = getattr(runtime.controller, "output", None)
                if "speech_control_enabled" in body and callable(getattr(output, "refresh_speech_controls", None)):
                    output.refresh_speech_controls("settings_changed")
                local_client = getattr(runtime.tts, "local", None)
                if callable(getattr(local_client, "configure_optimization", None)):
                    await local_client.configure_optimization(**optimization)
                await runtime.bus.publish(runtime.snapshot())
                return {"settings": {"tts": saved}, "effective_from": "next_synthesis" if "speech_control_enabled" in body else "next_answer", "pending": False,
                        "message": "合成设置已热更新，下一回答使用新参数。", "tts": runtime.local_tts_status_payload()}
        if not prepared:
            if runtime.application_initializing:
                raise HTTPException(status_code=409, detail="应用正在初始化，请稍后切换语音模型。")
            names = {row["id"]: row["name"] for row in runtime.local_tts_status_payload().get("models", [])}
            label = "语音合成 · " + ("MiniMax（在线）" if backend == "minimax" else names.get(local_model, local_model))
            async with runtime_operation(runtime, "tts_selection", label, pause=True, wait_output=True):
                return await apply_product_tts(body, prepared=True)
        if runtime.application_initializing:
            raise HTTPException(status_code=409, detail="应用组件仍在初始化，请等待初始化结束后再切换本地 TTS 模型。")
        async with runtime._session_lock:
            if runtime.state.listening:
                raise HTTPException(status_code=409, detail="监听尚未暂停，语音模型切换未执行，请重试。")
            if runtime.state.state.value not in {"IDLE", "ERROR", "LISTENING"}:
                raise HTTPException(status_code=409, detail="当前对话事务尚未结束，请等它结束后再切换本地 TTS 模型。")
            if runtime.tts is None:
                raise HTTPException(status_code=409, detail="TTS（语音合成）组件尚未初始化。")
            if runtime.tts_switch_busy():
                raise HTTPException(status_code=409, detail="仍有本地 TTS 会话正在处理或播报，请结束后再切换。")
            previous_tts_config = runtime.config.tts
            previous_model = previous_tts_config.local_model
            previous_backend = previous_tts_config.backend
            previous_options = {key: getattr(runtime.config.tts, key) for key in optimization}
            server = getattr(getattr(runtime.tts, "local", None), "server", None)
            refresh_status = getattr(server, "refresh_status", None)
            if callable(refresh_status):
                await refresh_status()
            current_status = runtime.local_tts_status_payload()
            if (all(getattr(runtime.config.tts, key) == value for key, value in optimization.items())
                    and all(getattr(runtime.config.tts, key) == value for key, value in online_config.items())
                    and backend == previous_backend and local_model == previous_model
                    and runtime.state.services.get("tts") == "READY"
                    and (backend == "minimax" or current_status.get("loaded_model") == local_model)):
                return {
                    "settings": {"tts": store.settings_only().get("tts", {})},
                    "effective_from": "immediate",
                    "pending": False,
                    "message": "所选 TTS（语音合成）已经就绪。",
                    "tts": current_status,
                }

            runtime.state.set_service("tts", "STARTING")
            logger.info("tts.selection_started backend=%s local_model=%s previous_backend=%s previous_model=%s",
                        backend, local_model, previous_backend, previous_model)
            await runtime.bus.publish(runtime.snapshot())
            try:
                # 新后端构造及预热均读取这份参数；从在线切到本地时也使用新步数。
                runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, **optimization, **online_config))
                local_client = getattr(runtime.tts, "local", None)
                if callable(getattr(local_client, "configure_optimization", None)):
                    await local_client.configure_optimization(**optimization)
                if optimization['local_model_revision'] is None:
                    await runtime.switch_tts_selection(backend, local_model)
                else:
                    await runtime.switch_tts_selection(backend, local_model, revision=optimization['local_model_revision'])
                saved = store.save_tts_local_model({"backend": backend, "local_model": local_model,
                                                    **optimization, **online_settings})
                runtime.tts.speech_control_enabled = optimization["speech_control_enabled"]
                output = getattr(runtime.controller, "output", None)
                if callable(getattr(output, "refresh_speech_controls", None)):
                    output.refresh_speech_controls("model_changed")
                runtime.config = replace(
                    runtime.config,
                    tts=replace(runtime.config.tts, backend=backend, local_model=local_model,
                                **optimization, **online_config),
                )
                runtime.state.set_service("tts", "READY")
                runtime._local_tts_load_state = "ready" if backend == "aniflive" else "disabled"
                runtime._local_tts_error = None
            except BaseException as exc:
                logger.exception("tts.selection_failed backend=%s local_model=%s", backend, local_model)
                try:
                    runtime.config = replace(runtime.config, tts=previous_tts_config)
                    local_client = getattr(runtime.tts, "local", None)
                    if callable(getattr(local_client, "configure_optimization", None)):
                        await local_client.configure_optimization(**previous_options)
                    if previous_tts_config.local_model_revision is None:
                        await runtime.switch_tts_selection(previous_backend, previous_model)
                    else:
                        await runtime.switch_tts_selection(previous_backend, previous_model, revision=previous_tts_config.local_model_revision)
                    runtime.config = replace(runtime.config, tts=previous_tts_config)
                    restored = True
                except Exception:
                    restored = False
                    logger.exception("tts.selection_rollback_failed")
                if not restored:
                    runtime_operations(runtime).intent(False)
                runtime.state.set_service("tts", "READY" if restored else "ERROR")
                from ..tts.voices import safe_voice_diagnostic
                runtime._local_tts_error = safe_voice_diagnostic(exc)
                await runtime.bus.publish(runtime.snapshot())
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise HTTPException(status_code=503, detail=safe_voice_diagnostic(exc)) from exc
            await runtime.bus.publish(runtime.snapshot())
            return {
                "settings": {"tts": saved},
                "effective_from": "immediate",
                "pending": False,
                "message": "TTS（语音合成）已切换并保存，将按原监听意图恢复。",
                "tts": runtime.local_tts_status_payload(),
            }

    async def apply_saved_product_settings(store, label: str) -> dict[str, Any]:
        """保存接口统一报告实际应用结果，加载期自动补应用而不等待对话。"""
        applied = await runtime.apply_product_settings(store.settings_only(), label=label)
        loading = runtime.application_initializing
        return {
            "effective_from": "immediate" if applied else "components_ready" if loading else "retry_required",  # 生效时机。
            "pending": not applied,  # 尚未应用：只在组件加载或应用失败时为真。
            "message": (f"{label}已保存并立即生效。" if applied else
                        f"{label}已保存，组件加载完成后自动生效。" if loading else
                        f"{label}已保存，但即时应用失败，请检查组件状态后重新保存。"),
        }

    @app.put("/api/v1/product/settings/screen-capture")
    async def save_screen_capture(payload: dict) -> dict:
        store = product_store_or_404()
        try:
            value = await asyncio.to_thread(store.save_screen_capture, payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"settings": {"screen_capture": value}, **await apply_saved_product_settings(store, "屏幕图像设置")}

    @app.put("/api/v1/product/settings/wake")
    async def update_product_wake(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await store.save_wake(body)
            return {
                "settings": value,
                **await apply_saved_product_settings(store, "KWS（关键词唤醒）设置"),
            }
        except Exception as exc:
            product_error = exc
            if isinstance(product_error, HTTPException):
                raise
            registry_error(product_error)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/quiet-hours")
    async def update_product_quiet_hours(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            if body.get("enabled") and not runtime.config.wake.enabled:
                raise ProductSettingsError("请先开启并保存 KWS（关键词唤醒）设置；组件加载完成后即可启用被动静默。")
            value = store.save_quiet_hours(body)
            await runtime.apply_quiet_hours(value["quiet_hours"])
            if runtime._pending_product_settings is not None:
                runtime.stage_product_settings(store.settings_only())
            return {"settings": value, "effective_from": "immediate", "pending": False,
                    "message": "被动静默时间已保存并即时生效。"}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/silence")
    async def update_product_silence(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await store.save_silence(body)
            return {
                "settings": value,
                **await apply_saved_product_settings(store, "静默设置"),
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/approvals")
    async def update_product_approvals(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            previous = store.settings_only()["approvals"]
            value = await asyncio.to_thread(store.save_approvals, body)
            try:
                async with runtime._session_lock:
                    runtime.configure_dsh_approvals()
                    session = runtime.shadow_session
                    if isinstance(session, DshShadowAgentSession):
                        await session.notify_voice_approval()
                    await runtime.bus.publish(runtime.snapshot())
            except Exception:
                await asyncio.to_thread(store.save_approvals, previous)
                runtime.configure_dsh_approvals()
                raise
            return {"settings": {"approvals": value}, "effective_from": "immediate", "pending": False,
                    "message": "授权确认设置已保存，连续提权仍需先明确同意本窗口的第一条请求。"}
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/shadow")
    async def update_product_shadow(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            previous = store.snapshot()["shadow"]["experimental_context_gate_enabled"]
            value = await asyncio.to_thread(store.save_shadow, body)
            try:
                enabled = await runtime.set_shadow_context_gate_enabled(
                    value["experimental_context_gate_enabled"],
                )
            except Exception:
                await asyncio.to_thread(store.save_shadow, {
                    "experimental_context_gate_enabled": previous,
                })
                raise
            # Keep the persisted overlay synchronized for future resets and
            # process restarts without staging a voice-session restart.
            store.config = runtime.config
            return {
                "settings": {"shadow": value},
                "effective_from": "immediate",
                "pending": False,
                "message": (
                    "实验性 Shadow 上下文门禁已开启。"
                    if enabled else "实验性 Shadow 上下文门禁已关闭，正文恢复直通。"
                ),
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/persona")
    async def update_product_persona(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await asyncio.to_thread(store.save_persona, body)
            return {
                "settings": {"persona": value.get("persona", {})},
                "effective_from": "next_session",  # effective_from（生效时机）：新会话。
                "pending": True,  # pending（待生效标记）：当前会话保留原人格与资料快照。
                "message": "人格与用户资料已保存；新会话开始后生效，当前会话继续使用原快照。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/custom-instructions")
    async def update_custom_instructions(body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"custom_instructions"}:
                raise ProductSettingsError("请求字段必须是 custom_instructions（自定义指令列表）。")
            store = product_store_or_404()
            value = await asyncio.to_thread(store.save_custom_instructions, body["custom_instructions"])
            await runtime.refresh_custom_instruction_settings()
            return {
                "settings": {"custom_instructions": value["custom_instructions"]},
                "effective_from": "next_request",
                "pending": False,
                "message": "自定义指令已保存，当前会话的后续模型请求立即使用新内容。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/speaker")
    async def update_product_speaker(body: dict[str, Any]) -> dict[str, Any]:
        async with runtime_operation(runtime, "speaker_settings", "声纹模式与参数"):
            return await apply_speaker_settings(body)

    async def apply_speaker_settings(body: dict[str, Any]) -> dict[str, Any]:
        speaker_audit("settings_submitted（设置提交）", submitted=parameter_snapshot(body))
        try:
            store = product_store_or_404()
            if getattr(runtime, "application_initializing", False) is True:
                saved = store.settings_only().get("speaker", {})
                enabling = (body.get("mode") == "filter" and saved.get("mode") != "filter") or (body.get("owner_only_barge_in") is True and not saved.get("owner_only_barge_in"))
                if enabling and not runtime.speaker_profile.available:
                    raise HTTPException(status_code=422, detail="未注册主人声纹前不能启用拦截或主人打断")
                value = await asyncio.to_thread(store.save_speaker, body)
                speaker_audit("settings_saved（初始化期间保存）", saved=value, reason="initializing")
                return {"settings": {"speaker": value}, **await apply_saved_product_settings(store, "声纹设置")}
            previous_speaker = store.settings_only().get("speaker", {
                "mode": "off",
                "confirm_threshold": runtime.config.speaker.confirm_threshold,
                "continuity_threshold": runtime.config.speaker.continuity_threshold,
                "continuity_relaxation": _speaker_relaxation(runtime.config.speaker),
                "owner_only_barge_in": runtime.config.speaker.owner_only_barge_in,
            })
            enabling = (body.get("mode") == "filter" and previous_speaker.get("mode") != "filter") or (body.get("owner_only_barge_in") is True and not previous_speaker.get("owner_only_barge_in"))
            if enabling:
                if not runtime.speaker_profile.available:
                    raise HTTPException(status_code=422, detail="未注册主人声纹前不能启用拦截或主人打断")
                if not await runtime.ensure_speaker_worker():
                    raise HTTPException(status_code=422, detail="声纹 Worker（工作进程）不可用，暂时不能启用拦截或主人打断")
            value = await asyncio.to_thread(store.save_speaker, body)
            speaker_audit("settings_saved（设置已保存）", saved=value)
            try:
                async with runtime._session_lock:
                    applied = await runtime.set_speaker_settings(value)
                    if runtime._pending_product_settings is not None:
                        runtime.stage_product_settings(store.settings_only())
            except Exception:
                # Runtime availability is part of the filter-mode contract;
                # restore the previous persisted mode when live application
                # cannot complete.
                await asyncio.to_thread(store.restore_speaker, previous_speaker)
                try:
                    async with runtime._session_lock:
                        await runtime.set_speaker_settings(previous_speaker, restoring=True)
                except Exception as rollback_error:
                    speaker_audit("runtime_rollback_failed（运行参数回滚失败）", reason=str(rollback_error)[:240])
                speaker_audit("settings_rollback（设置回滚）", saved=previous_speaker, reason="runtime_apply_failed")
                raise
            return {
                "settings": {"speaker": value},
                "effective_from": "next_heartbeat",
                "pending": False,
                "message": "判断参数从下一心跳生效；初始采集参数从下一次激活生效，已放行正文保持有效。",
                "runtime": applied,
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, (ValueError, TypeError)):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise

    @app.get("/api/v1/speaker/status")
    async def get_speaker_status() -> dict[str, Any]:
        return runtime.speaker_status()

    @app.get("/api/v1/speaker/profile")
    async def get_speaker_profile() -> dict[str, Any]:
        return runtime.speaker_profile_snapshot()

    @app.post("/api/v1/speaker/enrollment/start")
    async def start_speaker_enrollment() -> dict[str, Any]:
        return runtime.start_speaker_enrollment()

    @app.post("/api/v1/speaker/enrollment/{enrollment_id}/segment")
    async def add_speaker_enrollment_segment(enrollment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"index", "label", "pcm_base64"}:
                raise ValueError("录音段字段必须是 index、label、pcm_base64")
            return runtime.add_speaker_enrollment_segment(
                enrollment_id,
                index=body.get("index"),
                label=str(body.get("label") or "自定义"),
                encoded=body.get("pcm_base64"),
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/speaker/enrollment/{enrollment_id}/capture")
    async def capture_speaker_enrollment_segment(enrollment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Record one enrollment segment from the runtime input device."""
        try:
            if set(body) - {"index", "label", "seconds", "capture_id"} or not {"index", "label", "seconds"} <= set(body):
                raise ValueError("录制字段必须包含 index（段编号）、label（标签）、seconds（秒数），可选 capture_id（采集请求标识）")
            seconds = body.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("录制时长必须是数字")
            return await runtime.capture_speaker_enrollment_segment(
                enrollment_id,
                index=body.get("index"),
                label=str(body.get("label") or ""),
                seconds=float(seconds),
                capture_id=body.get("capture_id"),
            )
        except EnrollmentCaptureError as exc:
            raise HTTPException(status_code=422,
                                detail={"code": exc.code, "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def stop_capture(owner: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) - {"capture_id", "cancel"} or not isinstance(body.get("capture_id"), str) or not isinstance(body.get("cancel", False), bool):
                raise ValueError("需要 capture_id（采集请求标识）与可选 cancel（取消本段）")
            return await runtime.stop_speaker_capture(owner, body["capture_id"], cancel=body.get("cancel", False))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/speaker/enrollment/{enrollment_id}/capture/stop")
    async def stop_enrollment_capture(enrollment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await stop_capture(enrollment_id, body)

    @app.post("/api/v1/speaker/test/capture/stop")
    async def stop_test_capture(body: dict[str, Any]) -> dict[str, Any]:
        return await stop_capture("test", body)

    @app.post("/api/v1/speaker/enrollment/{enrollment_id}/commit")
    async def commit_speaker_enrollment(enrollment_id: str) -> dict[str, Any]:
        try:
            return await runtime.commit_speaker_enrollment(enrollment_id)
        except EnrollmentRejectedError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "SPEAKER_ENROLLMENT_INCONSISTENT",
                        "message": str(exc), **exc.detail},
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/v1/speaker/enrollment/{enrollment_id}")
    async def cancel_speaker_enrollment(enrollment_id: str) -> dict[str, Any]:
        runtime.cancel_speaker_enrollment(enrollment_id)
        return {"cancelled": True}

    @app.delete("/api/v1/speaker/templates/{template_id}")
    async def delete_speaker_template(template_id: str) -> dict[str, Any]:
        try:
            profile = await asyncio.to_thread(runtime.speaker_profile.delete_template, template_id)
            if runtime.speaker_router is not None:
                runtime.speaker_router.profile.load()
            # A profile is required for both filtering and owner-only barge in.
            # Keep the selected owner-only toggle on profile loss so runtime and
            # UI can report the degraded state instead of silently restoring
            # ordinary barge-in.
            current_speaker_settings = (
                product_settings.settings_only().get("speaker", {})
                if product_settings is not None else {}
            )
            if not profile.get("available") and (
                current_speaker_settings.get("mode", runtime.config.speaker.mode) == "filter"
                or current_speaker_settings.get("owner_only_barge_in") is True
            ):
                thresholds = {
                    "confirm_threshold": runtime.config.speaker.confirm_threshold,
                    "continuity_threshold": runtime.config.speaker.continuity_threshold,
                    "continuity_relaxation": _speaker_relaxation(runtime.config.speaker),
                }
                next_settings = {
                    "mode": "off" if current_speaker_settings.get("mode", runtime.config.speaker.mode) == "filter" else current_speaker_settings.get("mode", runtime.config.speaker.mode),
                    "owner_only_barge_in": bool(current_speaker_settings.get("owner_only_barge_in", False)),
                    **thresholds,
                }
                previous_settings = (
                    product_settings.settings_only().get("speaker")
                    if product_settings is not None else None
                )
                if product_settings is not None:
                    await asyncio.to_thread(product_settings.save_speaker, next_settings)
                try:
                    applied = await runtime.set_speaker_settings(next_settings)
                except Exception:
                    if product_settings is not None and isinstance(previous_settings, Mapping):
                        await asyncio.to_thread(product_settings.save_speaker, previous_settings)
                    raise
                response = dict(profile)
                response.update({"runtime": applied, "downgraded_to": next_settings["mode"]})
                return response
            response = dict(profile)
            response["downgraded_to"] = None
            return response
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/v1/speaker/templates/{template_id}")
    async def update_speaker_template(template_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"include_in_centroid"} or type(body.get("include_in_centroid")) is not bool:
                raise ValueError("模板设置字段必须是 include_in_centroid 布尔值")
            profile = await asyncio.to_thread(
                runtime.speaker_profile.set_template_centroid,
                template_id,
                body["include_in_centroid"],
            )
            if runtime.speaker_router is not None:
                runtime.speaker_router.profile.load()
            return profile
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/speaker/test")
    async def test_speaker(body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"pcm_base64"}:
                raise ValueError("现场测试字段必须是 pcm_base64")
            encoded = body.get("pcm_base64")
            if not isinstance(encoded, str):
                raise ValueError("pcm_base64 必须是字符串")
            return await runtime.test_speaker_audio(encoded)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/speaker/test/capture")
    async def capture_speaker_test(body: dict[str, Any]) -> dict[str, Any]:
        """Record and score a live test clip through the runtime chain."""
        try:
            if set(body) - {"seconds", "capture_id"} or "seconds" not in body:
                raise ValueError("录制字段必须是 seconds（秒数），可选 capture_id（采集请求标识）")
            return await runtime.capture_speaker_test(seconds=body.get("seconds"), capture_id=body.get("capture_id"))
        except EnrollmentCaptureError as exc:
            raise HTTPException(status_code=422,
                                detail={"code": exc.code, "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/models/registry")
    async def get_model_registry() -> dict[str, Any]:
        try:
            return await registry_or_404().snapshot()
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.get("/api/v1/models/credentials")
    async def get_fixed_model_credentials() -> dict[str, Any]:
        try:
            return await registry_or_404().fixed_credentials_snapshot()
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.get("/api/v1/models/credentials/{provider}/usage")
    async def get_fixed_provider_usage(provider: str) -> dict[str, Any]:
        try:
            return await registry_or_404().fixed_provider_usage(provider)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/models/credentials")
    async def save_fixed_model_credentials(body: dict[str, Any]) -> dict[str, Any]:
        try:
            registry = registry_or_404()
            result = await registry.save_fixed_credentials(body)
            await runtime.refresh_provider_credentials(registry)
            pending = getattr(runtime, "_minimax_dsh_sync_pending", False)
            await runtime.bus.publish(runtime.snapshot())
            return {**result, "dsh_sync_pending": pending,
                    "message": "接口密钥已保存，后续请求使用新密钥。" + ("后台凭据同步待处理，将自动重试。" if pending else "")}
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/settings/inference")
    async def save_inference(body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"backend"} or not isinstance(body.get("backend"), str) or body.get("backend") not in {"deepseek", "minimax"}:
            raise HTTPException(status_code=422, detail="只允许 backend（推理后端）：deepseek 或 minimax。")
        try:
            return await runtime.switch_inference(body["backend"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/models/scan")
    async def scan_models() -> dict[str, Any]:
        try:
            return await registry_or_404().scan()
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/validate")
    async def validate_model_provider(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().validate_connection(body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/providers")
    async def create_model_provider(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().create_verified_provider(body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/{model_id}/hide")
    async def hide_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().hide(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/{model_id}/restore")
    async def restore_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().restore(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.patch("/api/v1/models/{model_id}")
    async def update_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().update_model(model_id, body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.delete("/api/v1/models/{model_id}")
    async def delete_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().delete_model(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/assignments/apply")
    async def apply_model_assignments(body: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=409,
            detail="当前版本使用本地固定的 DeepSeek（语言模型）和 MiniMax（语音合成）配置，仅支持更新接口密钥。",
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        logger.info("websocket.connected peer=%s", websocket.client)
        sequencer = ProtocolSequencer(lambda: runtime.conversation.conversation_id)
        subscription = runtime.bus.subscribe()
        connection_id = uuid4().hex
        debug_events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        event_task = asyncio.create_task(_forward_events(websocket, subscription, sequencer), name="websocket-events")
        debug_task = asyncio.create_task(_forward_debug_events(websocket, debug_events, sequencer), name="websocket-shadow-debug")
        # Let the forwarding task register its queue before a fast
        # session.start command can publish the first turn event.
        await asyncio.sleep(0)
        try:
            while True:
                message = await websocket.receive_json()
                parsed = _valid_command(message)
                if parsed is None:
                    logger.warning("websocket.invalid_command peer=%s", websocket.client)
                    await _send_error(websocket, sequencer, "INVALID_COMMAND", "命令格式无效。", None, True)
                    continue
                command, _payload = parsed
                logger.info("websocket.command peer=%s command=%s", websocket.client, command)
                if (getattr(runtime, "application_initializing", False) is True
                        and command in {"session.start", "session.reset", "session.clear", "shadow.debug.wake"}):
                    await _send_error(websocket, sequencer, "RUNTIME_STARTING", "组件仍在加载，请等待启动完成。", None, True,
                                      request_id=message["request_id"])
                    continue
                if command == "subscribe":
                    await websocket.send_json(sequencer.envelope(runtime.snapshot()))
                elif command == "conversation.image.read":
                    cid = _payload.get("conversation_id")
                    aid = _payload.get("attachment_id")
                    item = runtime.history.screen_attachment(cid, aid) if cid == runtime.conversation.conversation_id and isinstance(aid, str) else None
                    if item is None:
                        await websocket.send_json(sequencer.envelope(event("conversation.image", request_id=message["request_id"],
                            conversation_id=cid, attachment_id=aid, error="图片不属于当前会话或已失效")))
                    else:
                        encoded = item["base64"]
                        chunk_size = 64 * 1024
                        total = (len(encoded) + chunk_size - 1) // chunk_size
                        for index in range(total):
                            await websocket.send_json(sequencer.envelope(event("conversation.image", request_id=message["request_id"],
                                conversation_id=cid, attachment_id=aid, index=index, total=total,
                                mime_type=item["mime_type"], data=encoded[index * chunk_size:(index + 1) * chunk_size])))
                            await asyncio.sleep(0)
                elif command == "session.start":
                    await runtime.start_listening()
                elif command == "session.stop":
                    await runtime.stop_listening()
                elif command in {"session.reset", "session.clear"}:
                    operation = "reset" if command == "session.reset" else "clear"
                    try:
                        if operation == "reset":
                            await runtime.reset_session()
                        else:
                            await runtime.clear_session()
                        await websocket.send_json(sequencer.envelope(event("session.operation",
                            request_id=message["request_id"], operation=operation, status="completed")))
                    except Exception:
                        logger.exception("handoff.manual_%s_failed", operation)
                        await _send_error(websocket, sequencer,
                            "HANDOFF_RESET_FAILED" if operation == "reset" else "SESSION_CLEAR_FAILED",
                            "会话操作未完成，已保存的材料与归档已保留，可重试以完成会话切换。", None, True,
                            request_id=message["request_id"])
                elif command == "session.reasoning_mode":
                    mode = _payload.get("mode")
                    if not isinstance(mode, str) or not await runtime.set_reasoning_mode(mode):
                        await _send_error(websocket, sequencer, "INVALID_REASONING_MODE", "运行模式必须是 fast（快速）或 deep（深度）。", None, True)
                elif command == "shadow.debug.wake":
                    async def debug_sink(items: list[ShadowEvent]) -> None:
                        for item in items:
                            await debug_events.put(event(
                                "shadow.debug.event", None,
                                event_id=item.event_id, kind=item.kind, text=item.text,
                                source=item.source, options=list(item.options),
                                failure_code=item.failure_code,
                                infrastructure=item.infrastructure,
                            ))
                    try:
                        result = runtime.debug_shadow_wake(connection_id, event_sink=debug_sink)
                        await debug_events.put(event(
                            "shadow.debug.wake", None, request_id=message["request_id"], **result,
                        ))
                    except ValueError as exc:
                        code = str(exc)
                        await _send_error(websocket, sequencer,
                                          code if code in {"SHADOW_DEBUG_DISABLED", "SHADOW_UNAVAILABLE"} else "INVALID_SHADOW_DEBUG_WAKE",
                                          "影子调试唤醒不可受理。", None, True)
                else:
                    await _send_error(websocket, sequencer, "UNKNOWN_COMMAND", f"不支持的命令：{command}", None, True,
                                      request_id=message["request_id"])
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.info("WebSocket closed: %s", exc)
        finally:
            logger.info("websocket.disconnected peer=%s", websocket.client)
            event_task.cancel()
            debug_task.cancel()
            await asyncio.gather(event_task, debug_task, return_exceptions=True)
            await runtime.close_shadow_debug(connection_id)

    return app


async def _forward_events(websocket: Any, subscription: Any, sequencer: ProtocolSequencer) -> None:
    try:
        async for value in subscription:
            if not sequencer.public(value):
                continue
            if value.type == "speech.completed":
                payload = value.payload if isinstance(value.payload, dict) else {}
                delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
                logger.info("websocket.speech_completed input_id=%s speech_id=%s status=%s delivery=%s",
                            payload.get("input_id") or value.turn_id, payload.get("speech_id"), payload.get("status"), delivery.get("status"))
            else:
                logger.debug("websocket.event type=%s input_id=%s", value.type, value.payload.get("input_id") or value.turn_id)
            await websocket.send_json(sequencer.envelope(value))
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await websocket.close(code=1013)
        except Exception:
            pass
        return


async def _forward_debug_events(websocket: Any, queue: asyncio.Queue[VoiceEvent], sequencer: ProtocolSequencer) -> None:
    try:
        while True:
            value = await queue.get()
            await websocket.send_json(sequencer.envelope(value))
    except asyncio.CancelledError:
        raise


async def _send_error(
    websocket: Any,
    sequencer: ProtocolSequencer,
    code: str,
    message: str,
    turn_id: str | None,
    recoverable: bool,
    *,
    request_id: str | None = None,  # request_id（失败操作的请求标识，供前端解除对应锁定）。
) -> None:
    await websocket.send_json(
        sequencer.envelope(event("error", turn_id, code=code, message=message, recoverable=recoverable,
                                 **({"request_id": request_id} if request_id else {})))
    )
