"""生产接口的调试事件及一次性介入。仅受管调试子进程获得本机通道凭据。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
from typing import Any, TypeVar

_ENV = {name: os.environ.pop(name, "") for name in ("VOICE_AGENT_PIPE", "VOICE_AGENT_TOKEN")}
_PIPE = _ENV["VOICE_AGENT_PIPE"]
_TOKEN = _ENV["VOICE_AGENT_TOKEN"]
ENABLED = os.name == "nt" and _PIPE.startswith(r"\\.\pipe\VoiceTerminal-Agent-") and len(_TOKEN) == 64
_LIMIT = 256 * 1024
_CALL_ID: ContextVar[int | None] = ContextVar("agent_debug_call_id", default=None)
T = TypeVar("T")


def worker_environment() -> dict[str, str]:
    return dict(_ENV) if ENABLED else {}


def _exchange(point: str, stage: str, value: Any, call_id: int | None) -> dict:
    try:
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "worker.point",
            "params": {"capability": _TOKEN, "point": point, "stage": stage,
                       "call_id": call_id, "data": value},
        }, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(request) >= _LIMIT:
            return {}
        with open(_PIPE, "r+b", buffering=0) as pipe:
            request += b"\n"
            remaining = memoryview(request)
            while remaining:
                written = pipe.write(remaining)
                if not written:
                    return {}
                remaining = remaining[written:]
            chunks = bytearray()
            while len(chunks) < _LIMIT:
                chunk = pipe.read(4096)
                if not chunk:
                    return {}
                chunks.extend(chunk)
                if chunks.endswith(b"\n"):
                    break
            reply = json.loads(chunks)
            result = reply.get("result")
            return result if reply.get("id") == 1 and isinstance(result, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def point_sync(
    point: str,
    stage: str,
    value: T,
    validate: Callable[[Any], bool] | None = None,
) -> tuple[T, int | None]:
    call_id = _CALL_ID.get()
    if not ENABLED:
        return value, call_id
    result = _exchange(point, stage, value, call_id)
    replacement = result.get("replacement")
    call_id = result.get("call_id", call_id)
    if result.get("replaced") is True:
        if validate is not None and validate(replacement):
            value = replacement
        else:
            _exchange(point, "rejected", {"reason": "替换数据未通过原业务边界约定。"}, call_id)
    return value, call_id


async def point(
    point: str,
    stage: str,
    value: T,
    validate: Callable[[Any], bool] | None = None,
) -> tuple[T, int | None]:
    if not ENABLED:
        return value, None
    return await asyncio.to_thread(point_sync, point, stage, value, validate)


def start_call(call_id: int | None):
    return _CALL_ID.set(call_id)


def end_call(token) -> None:
    _CALL_ID.reset(token)


def entry_call(value: str) -> int | None:
    return int(value) if len(value) <= 19 and value.isascii() and value.isdigit() else None


def observe_operation(function):
    """以生产函数名称登记共享边界，正常模式直接运行原函数。"""
    if not ENABLED:
        return function
    name = function.__qualname__
    signature = inspect.signature(function)

    @wraps(function)
    async def observed(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        arguments = {key: _summary(value) for key, value in bound.arguments.items() if key != "self"}
        _, call_id = await point(name, "input", arguments)
        token = start_call(call_id)
        try:
            result = await function(*args, **kwargs)
        except Exception as error:
            await point(name, "error", {"type": type(error).__name__})
            raise
        else:
            await point(name, "result", _summary(result))
            return result
        finally:
            end_call(token)

    return observed


def _summary(value: Any, depth: int = 0) -> Any:
    if depth >= 6:
        return None
    if isinstance(value, bytes):
        return {"bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): _summary(item, depth + 1) for key, item in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_summary(item, depth + 1) for item in value[:64]]
    if isinstance(value, str):
        return value[:16_384]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


def route_class():
    from fastapi.routing import APIRoute
    from starlette.requests import Request

    class ObservedRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def observed(request: Request):
                if not request.url.path.startswith("/api/v1/"):
                    return await handler(request)
                name = f"{request.method} {self.path}"
                body = None
                length = request.headers.get("content-length", "")
                if request.method in {"POST", "PUT", "PATCH", "DELETE"} and length.isdigit() \
                    and 0 < int(length) < _LIMIT and request.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0] == "application/json":
                    body_bytes = await request.body()
                    if len(body_bytes) < _LIMIT:
                        try:
                            body = json.loads(body_bytes)
                        except ValueError:
                            pass
                inputs = {"path": request.path_params, "query": dict(request.query_params), "body": body}
                owner = start_call(entry_call(request.headers.get("X-Voice-Agent-Call", "")))
                try:
                    edited, call_id = await point(name, "input", inputs, lambda value:
                        isinstance(value, dict) and value.get("path") == inputs["path"]
                        and value.get("query") == inputs["query"] and type(value.get("body")) is type(body)
                    )
                finally:
                    end_call(owner)
                if edited is not inputs and body is not None:
                    async def receive():
                        return {"type": "http.request", "body": json.dumps(edited["body"]).encode(), "more_body": False}

                    request = Request(request.scope, receive)
                token = start_call(call_id)
                try:
                    response = await handler(request)
                    result = {"status": response.status_code}
                    raw = getattr(response, "body", b"")
                    if 0 < len(raw) < _LIMIT and response.media_type == "application/json":
                        try:
                            result["data"] = json.loads(raw)
                        except ValueError:
                            pass
                    await point(name, "result", result)
                    return response
                except Exception as error:
                    await point(name, "error", {"type": type(error).__name__})
                    raise
                finally:
                    end_call(token)

            return observed

    return ObservedRoute
