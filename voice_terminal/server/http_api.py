"""HTTP and WebSocket routes for the session runtime."""
from __future__ import annotations
from dataclasses import replace

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any, Mapping
from urllib.parse import quote
from uuid import uuid4

from .. import __version__  # __version__（后端源码版本）：接口元数据与发布记录共用。
from ..core.events import VoiceEvent, event
from ..dsh import BRIDGE_PROTOCOL_VERSION, DshBridgeError, DshShadowAgentSession
from ..application.ports import validate_endpoint_url
from ..speaker.enrollment import EnrollmentRejectedError
from ..speaker.enrollment_capture import EnrollmentCaptureError
from ..model_registry.service import ModelRegistryError, ModelRegistryService
from ..model_registry.vault import CredentialVaultUnavailable
from .product import (
    PRODUCT_MINIMAX_TTS_MODELS, ProductSettingsError, ProductSettingsStore,
    _threshold_to_sensitivity, sanitize_runtime_snapshot,
)
from .snapshot import ProtocolSequencer, snapshot_event

logger = logging.getLogger(__name__)

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


async def _validate_minimax_voice_id(runtime: Any, voice_id: str) -> None:
    """Validate arbitrary official MiniMax voice_id（音色标识），不固定音色目录。"""
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
    if voice_id not in _minimax_voice_ids(payload):
        raise HTTPException(status_code=422, detail="该音色 ID 未出现在 MiniMax 官方音色查询结果中。")


def create_app(runtime: SessionRuntime, *, lifespan: Any = None,
               model_registry: ModelRegistryService | None = None,
               product_settings: ProductSettingsStore | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi，请先运行 scripts\\setup.ps1。")

    app = FastAPI(title="Voice Terminal Server", version=__version__, lifespan=lifespan)
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

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health_payload()

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
                      ("analysis_available", "VOICE_APP_ANALYSIS_AVAILABLE"))
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
        runtime_payload = sanitize_runtime_snapshot(runtime.snapshot().payload)
        # Keep the response field for existing clients without reviving the
        # archived local synthesizer or inspecting its model directory.
        runtime_payload["tts_worker"] = {"state": "stopped"}
        conversation = runtime_payload.pop("conversation", {"recent": [], "current": {}})
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
        if not service.config.enabled:
            raise HTTPException(status_code=409, detail="屏幕图像设置已关闭，无法捕获屏幕。")
        if not service.available:
            raise HTTPException(status_code=503, detail="屏幕捕获组件当前不可用。")
        generation = service.generation
        lease_generation = service.lease_generation("dsh", activation_id)
        try:
            packet = await service.grab(source="dsh", scope=activation_id)
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

    @app.post("/api/internal/dsh-bridge/shadow.ambient.read")
    async def dsh_shadow_ambient_read(request: Request) -> JSONResponse:
        payload, _session = await dsh_callback_body(request)
        limit = payload.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise HTTPException(status_code=422, detail="invalid limit")
        return JSONResponse(
            {
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "result": "accepted",
                "source": "ambient_untrusted",
                **runtime.ambient_store.snapshot(limit),
            },
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
        rules = [key for key in ("at", "daily", "weekly") if key in body]
        if len(rules) != 1 or set(body) != {"text", rules[0]}:
            raise HTTPException(status_code=422, detail="仅允许 text（任务说明）和一种单次、每天或每周规则。")
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise HTTPException(status_code=422, detail="任务说明须为 1 至 500 字。")
        return await dsh_task_api("POST", "tasks", {
            "text": text.strip(), rules[0]: body[rules[0]],
            "target": "shadow_agent", "reference": "", "creation_source": "manual",
        })

    @app.post("/internal/application/dsh-approval")
    async def decide_dsh_approval(request: Request) -> dict[str, Any]:
        """权限决定仅经本次桌面宿主认证转发，内部凭据不传到页面。"""
        authorize_application(request)
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"approval_id", "decision", "run_seq"}
                or not isinstance(body.get("approval_id"), str) or not 1 <= len(body["approval_id"]) <= 160
                or body.get("decision") not in {"allowed-once", "allowed-until-silence", "rejected"}
                or type(body.get("run_seq")) is not int or body["run_seq"] < 0):
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

    @app.put("/api/v1/product/settings/hotwords")
    async def save_hotwords(body: dict[str, Any]) -> dict:
        service = runtime.hotwords
        try:
            async with service.update_lock:
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
            store = product_store_or_404()
            if set(body) != {"devices"} or not isinstance(body.get("devices"), dict):
                raise ProductSettingsError("计算设备设置字段必须仅包含 devices（模型设备选项）。")
            # Resolve through the historical module so existing diagnostics
            # and test hooks observe the same hardware probe.
            from . import websocket as session_module
            from ..compute import model_hardware_status
            hardware = await asyncio.to_thread(session_module.hardware_status, refresh=True) if any(mode == "cuda" for mode in body["devices"].values()) else await asyncio.to_thread(session_module.hardware_status)
            hardware = await asyncio.to_thread(model_hardware_status, hardware, runtime.config, verify=True)
            for name, mode in body["devices"].items():
                if mode == "cuda" and not hardware.get("models", {}).get(name, {}).get("gpu_available"):
                    reason = hardware.get("models", {}).get(name, {}).get("reason") or "GPU 后端不可用。"
                    raise ProductSettingsError(f"{name} 暂不能使用 GPU：{reason}")
            current = await runtime.compute_status()
            active_devices = {
                name: str(item.get("device") or "cpu")
                for name, item in current["models"].items()
            }
            value = await asyncio.to_thread(store.save_compute, body, active_devices=active_devices)
            return {
                "settings": {"compute": value},
                "effective_from": "application_restart",
                "restart_required": True,
                "pending": True,
                "message": "本地模型运行设备设置已保存，重新启动应用后生效。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
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

    def tts_voice_library_or_503(model=None):
        libraries = getattr(runtime, "voice_library", None)
        if libraries is None:
            raise HTTPException(status_code=503, detail="音色库不可用。")
        try:
            return libraries.for_model(model) if model else libraries
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def tts_voice_operations_or_503():
        operations = getattr(runtime, "tts_voice_operations", None)
        if operations is None:
            raise HTTPException(status_code=503, detail="音色操作服务未就绪。")
        return operations

    async def start_tts_voice_operation(callback):
        if runtime.application_initializing:
            raise HTTPException(status_code=409, detail="应用正在初始化，请稍后操作。")
        async with runtime._session_lock:
            if runtime.state.listening or runtime.state.state.value not in {"IDLE", "ERROR"}:
                raise HTTPException(status_code=409, detail="请先结束监听与当前播报，再操作音色。")
            if runtime.tts is not None and runtime.tts.active_local_sessions:
                raise HTTPException(status_code=409, detail="本地合成仍在进行，请稍后重试。")
            try:
                return JSONResponse(callback(), status_code=202)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tts/voices/{model}")
    async def get_tts_voices(model: str):
        library = tts_voice_library_or_503(model)
        return {"voices": library.list_voices({model: library.active_selection}),
                "active_voice": library.active_selection, "model": model}

    @app.post("/api/v1/tts/voices/{model}")
    async def create_tts_voice(model: str, body: dict[str, Any]):
        try:
            validate_tts_candidate_steps(model, body)
            voice = await asyncio.to_thread(tts_voice_library_or_503(model).create_voice, body)
            return {"voice": voice}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    @app.get("/api/v1/tts/assets/{model}/{asset_id}/audio")
    async def get_tts_asset_audio(model: str, asset_id: str):
        try:
            data = await asyncio.to_thread(tts_voice_library_or_503(model).asset_bytes, asset_id)
            return Response(content=data, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
            return tts_voice_operations_or_503().cancel(operation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="音色操作不存在或已过期。") from exc

    @app.put("/api/v1/product/settings/tts")
    async def update_product_tts(body: dict[str, Any]) -> dict[str, Any]:
        return await apply_product_tts(body)

    async def apply_product_tts(body: dict[str, Any], *, after_drain: bool = False) -> dict[str, Any]:
        store = product_store_or_404()
        if not body or set(body) - {
            "backend", "local_model", "online_model", "online_voice_id",
            "local_optimization", "acoustic_continuation", "inference_steps_by_model",
        }:
            raise HTTPException(
                status_code=422,
                detail="TTS 设置包含不支持的字段。",
            )
        optimization = {field: body.get(field, getattr(runtime.config.tts, field))
                        for field in ("local_optimization", "acoustic_continuation")}
        if any(not isinstance(value, bool) for value in optimization.values()):
            raise HTTPException(status_code=422, detail="本地优化与声学续接开关必须为布尔值。")
        from ..config import validate_tts_inference_steps
        try:
            optimization["inference_steps_by_model"] = validate_tts_inference_steps(
                body.get("inference_steps_by_model", {}), runtime.config.tts.inference_steps_by_model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        optimization["local_optimization"] = True  # 旧字段不再选择通用提前合成调度。
        local_model = body.get("local_model", runtime.config.tts.local_model)
        backend = body.get("backend", "voxcpm1" if "local_model" in body else runtime.config.tts.backend)
        if not isinstance(backend, str) or backend not in {"minimax", "voxcpm1"}:
            raise HTTPException(status_code=422, detail="backend（语音后端）必须为 minimax 或 voxcpm1。")
        if not isinstance(local_model, str) or local_model not in {"voxcpm1", "voxcpm2"}:
            raise HTTPException(
                status_code=422,
                detail="local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2。",
            )
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
            if not after_drain:
                await _validate_minimax_voice_id(runtime, online_voice_id)
            online_settings = {"online_model": online_model, "online_voice_id": online_voice_id}
            online_config = {"model": online_model, "voice_id": online_voice_id}
        if not selection_requested and not online_requested:
            async with runtime._session_lock:
                saved = store.save_tts_local_model(optimization)
                if getattr(runtime, "_pending_tts_update", None) is not None:
                    runtime._pending_tts_update.update(optimization)
                runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, **optimization))
                local_client = getattr(runtime.tts, "local", None)
                if local_client is not None:
                    for key, value in optimization.items():
                        setattr(local_client, key, value)
                    local_client._warmed = False
                await runtime.bus.publish(runtime.snapshot())
                return {"settings": {"tts": saved}, "effective_from": "next_answer", "pending": False,
                        "message": "合成设置已热更新，下一回答使用新参数。", "tts": runtime.local_tts_status_payload()}
        if not after_drain and (runtime.application_initializing or runtime.state.listening
                or runtime.state.state.value not in {"IDLE", "ERROR"}
                or runtime.tts_switch_busy()
                or (getattr(runtime, "tts_voice_operations", None) and runtime.tts_voice_operations.busy)
                or (getattr(runtime, "_tts_update_task", None) and not runtime._tts_update_task.done())):
            if runtime.tts is not None:
                server = getattr(getattr(runtime.tts, "local", None), "server", None)
                if backend == "voxcpm1" and callable(getattr(server, "model_available", None)) and not server.model_available(local_model):
                    raise HTTPException(status_code=422, detail="所选本地模型文件缺失。")
            pending_body = {"backend": backend, "local_model": local_model, **optimization, **online_settings}
            saved = store.save_tts_local_model(pending_body)
            runtime._pending_tts_update = pending_body  # 多次热切换保留最新一次选择。
            if not getattr(runtime, "_tts_update_task", None) or runtime._tts_update_task.done():
                runtime._tts_update_task = asyncio.create_task(drain_tts_updates(), name="tts-hot-update")
            return {"settings": {"tts": saved}, "effective_from": "after_current_answer", "pending": True,
                    "message": "选择已保存，当前音频事务结束后切换；主动停止监听后保持停止。",
                    "tts": runtime.local_tts_status_payload()}
        if runtime.application_initializing:
            raise HTTPException(status_code=409, detail="应用组件仍在初始化，请等待初始化结束后再切换本地 TTS 模型。")
        async with runtime._session_lock:
            if getattr(runtime, "tts_voice_operations", None) and runtime.tts_voice_operations.busy:
                raise HTTPException(status_code=409, detail="音色录音、生成或试听正在进行，请结束后再切换语音模型。")
            if runtime.state.listening:
                raise HTTPException(status_code=409, detail="请先停止监听，再切换本地 TTS 模型。")
            if runtime.state.state.value not in {"IDLE", "ERROR"}:
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
                    "message": "所选 TTS（语音合成）已经就绪；监听保持停止。",
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
                if local_client is not None:
                    for key, value in optimization.items():
                        setattr(local_client, key, value)
                    if optimization["inference_steps_by_model"] != previous_options["inference_steps_by_model"]:
                        local_client._warmed = False
                await runtime.switch_tts_selection(backend, local_model)
                if after_drain and getattr(runtime, "_pending_tts_update", None) is not None:
                    saved = store.settings_only()["tts"]  # 后来的选择已保存，不被中间切换覆盖。
                else:
                    saved = store.save_tts_local_model({"backend": backend, "local_model": local_model,
                                                        **optimization, **online_settings})
                runtime.config = replace(
                    runtime.config,
                    tts=replace(runtime.config.tts, backend=backend, local_model=local_model,
                                **optimization, **online_config),
                )
                runtime.state.set_service("tts", "READY")
                runtime._local_tts_load_state = "ready" if backend == "voxcpm1" else "disabled"
                runtime._local_tts_error = None
            except Exception as exc:
                logger.exception("tts.selection_failed backend=%s local_model=%s", backend, local_model)
                try:
                    runtime.config = replace(runtime.config, tts=previous_tts_config)
                    local_client = getattr(runtime.tts, "local", None)
                    if local_client is not None:
                        for key, value in previous_options.items():
                            setattr(local_client, key, value)
                        local_client._warmed = False
                    await runtime.switch_tts_selection(previous_backend, previous_model)
                    runtime.config = replace(runtime.config, tts=previous_tts_config)
                    restored = True
                except Exception:
                    restored = False
                    logger.exception("tts.selection_rollback_failed")
                runtime.state.set_service("tts", "READY" if restored else "ERROR")
                from ..tts.voices import safe_voice_diagnostic
                runtime._local_tts_error = safe_voice_diagnostic(exc)
                await runtime.bus.publish(runtime.snapshot())
                raise HTTPException(status_code=503, detail=safe_voice_diagnostic(exc)) from exc
            await runtime.bus.publish(runtime.snapshot())
            return {
                "settings": {"tts": saved},
                "effective_from": "immediate",
                "pending": False,
                "message": "TTS（语音合成）已切换并保存；监听保持停止，请手动恢复。",
                "tts": runtime.local_tts_status_payload(),
            }

    async def drain_tts_updates() -> None:
        """等待完整播报结算，再短暂停止输入完成模型热切换，不取消当前回答。"""
        store = product_store_or_404()
        resume_listening = False
        while getattr(runtime, "_pending_tts_update", None) is not None:
            await asyncio.sleep(0.1)
            if runtime.application_initializing or runtime.state.state.value not in {"IDLE", "ERROR", "LISTENING"}:
                continue
            if runtime.tts_switch_busy() or (getattr(runtime, "tts_voice_operations", None) and runtime.tts_voice_operations.busy):
                continue
            if getattr(runtime.controller, "_active_input", False):
                continue  # 等用户当前输入结算，热切换不丢弃正在识别的问题。
            previous_listening = runtime.state.listening
            resume_listening = resume_listening or previous_listening
            previous_selection = {
                "backend": runtime.config.tts.backend,
                "local_model": runtime.config.tts.local_model,
                "online_model": runtime.config.tts.model,
                "online_voice_id": runtime.config.tts.voice_id,
            }
            async with runtime._session_lock:
                if runtime.state.state.value not in {"IDLE", "ERROR", "LISTENING"} or runtime.tts_switch_busy() or getattr(runtime.controller, "_active_input", False):
                    continue
                if previous_listening and runtime.controller is not None:
                    try:
                        await runtime.controller.stop_listening()
                    except Exception as exc:
                        runtime._pending_tts_update = None
                        store.save_tts_local_model(previous_selection)
                        latest_options = {key: store.settings_only()["tts"][key] for key in (
                            "local_optimization", "acoustic_continuation", "inference_steps_by_model",
                        )}
                        runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, **latest_options))
                        local_client = getattr(runtime.tts, "local", None)
                        if local_client is not None:
                            for key, value in latest_options.items():
                                setattr(local_client, key, value)
                            local_client._warmed = False
                        from ..tts.voices import safe_voice_diagnostic
                        await runtime.bus.publish(event("error", code="TTS_HOT_UPDATE_FAILED", message="暂停监听失败，已保留原模型，请重新开始监听。" + safe_voice_diagnostic(exc), recoverable=True))
                        await runtime.bus.publish(runtime.snapshot())
                        return
            pending_body = runtime._pending_tts_update
            # 等待期间修改步数/优化开关时，使用最近已保存值，防止旧排队请求覆盖热更新。
            pending_body.update({key: store.settings_only()["tts"][key] for key in (
                "local_optimization", "acoustic_continuation", "inference_steps_by_model",
                "online_model", "online_voice_id",
            )})
            runtime._applying_tts_selection = pending_body
            runtime._pending_tts_update = None
            try:
                await apply_product_tts(pending_body, after_drain=True)
            except asyncio.CancelledError:
                resume_listening = False  # 应用关闭不重新启动监听。
                runtime._tts_resume_requested = False
                runtime._pending_tts_update = None
                raise
            except Exception as exc:
                if runtime._pending_tts_update is None:
                    store.save_tts_local_model(previous_selection)
                # 模型失败不撤销已授权的步数/提前合成更新；使运行值与保存值一致。
                latest_options = {key: store.settings_only()["tts"][key] for key in (
                    "local_optimization", "acoustic_continuation", "inference_steps_by_model",
                )}
                runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, **latest_options))
                local_client = getattr(runtime.tts, "local", None)
                if local_client is not None:
                    for key, value in latest_options.items():
                        setattr(local_client, key, value)
                    local_client._warmed = False
                from ..tts.voices import safe_voice_diagnostic
                await runtime.bus.publish(event("error", code="TTS_HOT_UPDATE_FAILED", message=safe_voice_diagnostic(exc), recoverable=True))
            finally:
                runtime._applying_tts_selection = None
                if (resume_listening or getattr(runtime, "_tts_resume_requested", False)) and runtime._pending_tts_update is None and getattr(runtime, "_tts_resume_requested", None) is not False:
                    await runtime.start_listening()
                    resume_listening = False
                if runtime._pending_tts_update is None:
                    runtime._tts_resume_requested = None
                await runtime.bus.publish(runtime.snapshot())

    async def apply_saved_product_settings(store, label: str) -> dict[str, Any]:
        """保存接口统一报告实际应用结果，加载期自动补应用而不等待对话。"""
        applied = await runtime.apply_product_settings(store.settings_only())
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
        from ..screen_capture import ScreenCaptureConfig
        store = product_store_or_404()
        try:
            value = ScreenCaptureConfig(**payload).snapshot()
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        previous = dict(store._settings)
        try:
            store._settings = {**store._settings, "screen_capture": value, "schema_version": 13}
            store._save_settings()
        except Exception:
            store._settings = previous
            raise
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
        try:
            store = product_store_or_404()
            if getattr(runtime, "application_initializing", False) is True:
                saved = store.settings_only().get("speaker", {})
                enabling = (body.get("mode") == "filter" and saved.get("mode") != "filter") or (body.get("owner_only_barge_in") is True and not saved.get("owner_only_barge_in"))
                if enabling and not runtime.speaker_profile.available:
                    raise HTTPException(status_code=422, detail="未注册主人声纹前不能启用拦截或主人打断")
                value = await asyncio.to_thread(store.save_speaker, body)
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
            try:
                async with runtime._session_lock:
                    applied = await runtime.set_speaker_settings(value)
                    if runtime._pending_product_settings is not None:
                        runtime.stage_product_settings(store.settings_only())
            except Exception:
                # Runtime availability is part of the filter-mode contract;
                # restore the previous persisted mode when live application
                # cannot complete.
                await asyncio.to_thread(store.save_speaker, previous_speaker)
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

    @app.get("/api/v1/ambient-context")
    async def get_ambient_context(limit: int = 20) -> dict[str, Any]:
        return runtime.ambient_store.snapshot(limit)

    @app.delete("/api/v1/ambient-context")
    async def clear_ambient_context() -> dict[str, Any]:
        runtime.ambient_store.clear()
        return runtime.ambient_store.snapshot()

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
            return await registry_or_404().save_fixed_credentials(body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

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
