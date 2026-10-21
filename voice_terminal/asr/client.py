from __future__ import annotations

import asyncio
import json
import os
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .protocol import AudioChunk

logger = logging.getLogger(__name__)


class AsrWorkerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "ASR_WORKER_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


ResultCallback = Callable[[dict[str, Any]], Awaitable[None]]


class AsrWorkerClient:
    def __init__(self, url: str, *, on_result: ResultCallback | None = None) -> None:
        self.url = url
        self._local_worker = urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}  # local_worker（本机工作进程）。
        self.on_result = on_result
        self._socket: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._health_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sent_count_by_turn: dict[str, int] = {}
        self._sent_bytes_by_turn: dict[str, int] = {}
        self._last_log_at_by_turn: dict[str, float] = {}

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def connect(self) -> None:
        if os.environ.get("VOICE_APP_ASR_AVAILABLE") == "0":
            raise AsrWorkerError("应用未确认语音识别工作进程所有权或就绪状态", code="ASR_WORKER_UNOWNED")
        if self._socket is not None:
            return
        try:
            import websockets  # type: ignore

            self._socket = await websockets.connect(self.url, open_timeout=5, ping_interval=20)
            self._receive_task = asyncio.create_task(self._receive_loop(), name="asr-worker-receive")
            logger.info("asr.client.connected url=%s", self.url)
        except Exception as exc:
            self._socket = None
            logger.exception("asr.client.connect_failed url=%s", self.url)
            raise AsrWorkerError(f"ASR Worker 连接失败：{exc}") from exc

    async def _receive_loop(self) -> None:
        socket = self._socket
        if socket is None:
            return
        cancelled = False
        disconnected = False
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("type") == "health":
                    request_id = message.get("request_id")
                    waiter = self._health_waiters.get(request_id) if isinstance(request_id, str) else None
                    # Older workers did not echo a request ID. Accept
                    # that response only when there is exactly one pending
                    # health check; normal requests always require a match.
                    if waiter is None and request_id is None and len(self._health_waiters) == 1:
                        waiter = next(iter(self._health_waiters.values()))
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                    else:
                        logger.warning("asr.client.unmatched_health_response request_id=%s", request_id)
                        if self._health_waiters:
                            # A response for a different request cannot be
                            # safely associated with any readiness check.
                            # Fail pending checks explicitly instead of
                            # turning a protocol violation into a timeout.
                            self._fail_health_waiters(
                                AsrWorkerError(
                                    "ASR Worker health 响应 request_id 不匹配",
                                    code="ASR_WORKER_PROTOCOL_ERROR",
                                )
                            )
                    continue
                if isinstance(message, dict) and message.get("type") == "error":
                    request_id = message.get("request_id")
                    waiter = self._health_waiters.get(request_id) if isinstance(request_id, str) else None
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                        continue
                    if self._health_waiters:
                        # A health-related error must be associated with the
                        # request that is waiting for it.  Treat an unrelated
                        # or missing ID as a protocol failure immediately;
                        # otherwise readiness would be reported only after a
                        # timeout and the transport error could be mistaken
                        # for a slow model.
                        self._fail_health_waiters(
                            AsrWorkerError(
                                "ASR Worker error 响应 request_id 不匹配",
                                code="ASR_WORKER_PROTOCOL_ERROR",
                            )
                        )
                        continue
                if self.on_result is not None and isinstance(message, dict):
                    logger.info(
                        "asr.client.received type=%s turn_id=%s text_len=%d text=%r latency=%s",
                        message.get("type"),
                        message.get("turn_id"),
                        len(message.get("text", "")) if isinstance(message.get("text"), str) else 0,
                        message.get("text", "")[:120] if isinstance(message.get("text"), str) else "",
                        json.dumps(message.get("latency"), ensure_ascii=False),  # latency（识别分段耗时）。
                    )
                    try:
                        await self.on_result(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # A controller callback bug must not be mislabeled as
                        # a transport disconnect or tear down the ASR socket.
                        logger.exception("asr.client.result_callback_failed")
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            disconnected = True
            logger.exception("asr.client.receive_failed")
            self._fail_health_waiters(AsrWorkerError(f"ASR Worker 连接断开：{exc}", code="ASR_WORKER_DISCONNECTED"))
            if self.on_result is not None:
                await self.on_result({"type": "error", "code": "ASR_WORKER_DISCONNECTED", "message": str(exc)})
        finally:
            if self._socket is socket:
                self._socket = None
                if not cancelled and not disconnected:
                    # A clean remote close still invalidates an established
                    # Worker connection.  Do not leave a health Future waiting
                    # until its timeout, and surface the same recoverable
                    # event as an exceptional close.
                    error = AsrWorkerError("ASR Worker 连接已关闭", code="ASR_WORKER_DISCONNECTED")
                    self._fail_health_waiters(error)
                    if self.on_result is not None:
                        await self.on_result({"type": "error", "code": error.code, "message": str(error)})
            logger.info("asr.client.disconnected url=%s", self.url)

    def _fail_health_waiters(self, error: BaseException) -> None:
        for waiter in tuple(self._health_waiters.values()):
            if not waiter.done():
                waiter.set_exception(error)

    async def _send(self, payload: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise AsrWorkerError("ASR Worker 尚未连接", code="ASR_WORKER_UNAVAILABLE")
        try:
            async with self._send_lock:
                if payload.get("type") == "audio.chunk" and self._local_worker:
                    # 在获得发送锁后记录本机时间，排队指标不混入客户端锁等待。
                    payload["sent_at_monotonic_ns"] = time.monotonic_ns()
                await socket.send(json.dumps(payload, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed write means the established Worker connection is no
            # longer usable.  Preserve a stable service code for the
            # controller while the receive loop reports the same transition.
            raise AsrWorkerError(f"ASR Worker 发送失败：{exc}", code="ASR_WORKER_DISCONNECTED") from exc
        logger.debug(
            "asr.client.sent type=%s turn_id=%s bytes=%d final=%s",
            payload.get("type"),
            payload.get("turn_id"),
            len(payload.get("pcm_base64", "")) if isinstance(payload.get("pcm_base64"), str) else 0,
            payload.get("is_final") is True,
        )

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._send(chunk.to_dict())
        turn_id = chunk.turn_id
        self._sent_count_by_turn[turn_id] = self._sent_count_by_turn.get(turn_id, 0) + 1
        self._sent_bytes_by_turn[turn_id] = self._sent_bytes_by_turn.get(turn_id, 0) + len(chunk.pcm)
        now = time.monotonic()
        last_log_at = self._last_log_at_by_turn.get(turn_id, 0.0)
        if chunk.is_final or not last_log_at or now - last_log_at >= 1.0:
            logger.info(
                "asr.client.audio_progress turn_id=%s chunk_count=%d bytes=%d final=%s",
                turn_id,
                self._sent_count_by_turn[turn_id],
                self._sent_bytes_by_turn[turn_id],
                chunk.is_final,
            )
            self._last_log_at_by_turn[turn_id] = now

    async def send_finish(self, turn_id: str) -> None:
        await self._send({"type": "turn.finish", "turn_id": turn_id})
        logger.info(
            "asr.client.turn_finish turn_id=%s chunks=%d bytes=%d",
            turn_id,
            self._sent_count_by_turn.pop(turn_id, 0),
            self._sent_bytes_by_turn.pop(turn_id, 0),
        )
        self._last_log_at_by_turn.pop(turn_id, None)

    async def configure_enhancements(self, dual_pass: bool) -> dict:
        """configure_enhancements（停止监听后的模型切换请求）。"""
        await self.connect()
        request_id = uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self._health_waiters[request_id] = waiter
        try:
            await self._send({"type": "enhancements.configure", "request_id": request_id,
                              "dual_pass": dual_pass})
            result = await asyncio.wait_for(waiter, 240)
            if result.get("type") == "error":
                raise AsrWorkerError(str(result.get("message")))
            return result
        finally:
            self._health_waiters.pop(request_id, None)

    async def configure_compute(self, device: str, *, allow_cuda_fallback: bool = False) -> dict:  # allow_cuda_fallback（恢复旧偏好时允许 GPU 回退到原实际设备）。
        """替换 Paraformer 模型子进程并核验设备与预热状态。"""
        await self.connect()
        request_id = uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self._health_waiters[request_id] = waiter
        try:
            await self._send({"type": "compute.configure", "request_id": request_id,
                              "device": device,
                              "allow_cuda_fallback": allow_cuda_fallback})  # device（目标设备模式）；allow_cuda_fallback（回滚时允许旧 GPU 偏好恢复到原 CPU 实际设备）。
            result = await asyncio.wait_for(waiter, 300)
            if result.get("type") == "error":
                raise AsrWorkerError(str(result.get("message")), code="ASR_COMPUTE_SWITCH_FAILED")
            return result
        finally:
            self._health_waiters.pop(request_id, None)

    async def transcribe_reference(self, pcm: bytes) -> dict:
        """transcribe_reference（人工上传参考音频转写），与实时 VAD（语音活动检测）路线分离。"""
        import base64
        await self.connect()
        request_id = uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self._health_waiters[request_id] = waiter
        try:
            await self._send({"type": "reference.transcribe", "request_id": request_id,
                              "turn_id": request_id, "pcm_base64": base64.b64encode(pcm).decode("ascii")})
            response = await asyncio.wait_for(waiter, 30)
            if response.get("type") == "error":
                raise AsrWorkerError(str(response.get("message")))
            return response["result"]
        finally:
            self._health_waiters.pop(request_id, None)

    async def health(self, timeout_s: float = 2.0,
                     *, allow_starting: bool = False) -> dict[str, Any]:  # allow_starting（允许返回启动中快照）。
        """Perform an application-level readiness check.

        Opening a WebSocket only proves that something is listening on the
        configured endpoint.  The Worker must echo this request after its
        model has loaded before callers may mark ASR READY. Status callers may
        opt into receiving a correlated STARTING snapshot during model rebuild.
        """

        if self._socket is None:
            raise AsrWorkerError("ASR Worker 尚未连接")
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._health_waiters[request_id] = waiter
        try:
            try:
                await self._send({"type": "health", "request_id": request_id})
                response = await asyncio.wait_for(waiter, timeout=max(0.05, float(timeout_s)))
            except asyncio.TimeoutError as exc:
                raise AsrWorkerError(f"ASR Worker health 超时（{timeout_s:g}s）", code="ASR_WORKER_TIMEOUT") from exc
            except asyncio.CancelledError:
                raise
            except AsrWorkerError:
                raise
            except Exception as exc:
                raise AsrWorkerError(f"ASR Worker health 失败：{exc}", code="ASR_WORKER_PROTOCOL_ERROR") from exc
        finally:
            self._health_waiters.pop(request_id, None)
            if not waiter.done():
                waiter.cancel()

        if response.get("type") == "error":
            raise AsrWorkerError(
                str(response.get("message") or "ASR Worker health 失败"),
                code=str(response.get("code") or "ASR_WORKER_NOT_READY"),
            )
        if allow_starting and response.get("status") == "STARTING" and response.get("model_loaded") is False:
            return response
        if response.get("status") != "READY":
            status = response.get("status", "UNKNOWN")
            raise AsrWorkerError(f"ASR Worker 未就绪：{status}", code="ASR_WORKER_NOT_READY")
        if response.get("model_loaded") is not True:
            code = "ASR_WORKER_PROTOCOL_ERROR" if "model_loaded" not in response else "ASR_WORKER_NOT_READY"
            raise AsrWorkerError("ASR Worker health 缺少有效的 model_loaded", code=code)
        return response

    async def close(self) -> None:
        self._fail_health_waiters(AsrWorkerError("ASR Worker 客户端已关闭", code="ASR_WORKER_DISCONNECTED"))
        task, self._receive_task = self._receive_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()
        self._health_waiters.clear()
        logger.info("asr.client.closed url=%s", self.url)
