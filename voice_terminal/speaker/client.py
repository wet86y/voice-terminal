from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import SpeakerObservation

logger = logging.getLogger(__name__)

ObservationCallback = Callable[[SpeakerObservation], Awaitable[None]]


class SpeakerWorkerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SPEAKER_WORKER_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


class SpeakerWorkerClient:
    """Bounded, fail-open client for the optional Speaker Worker."""

    def __init__(self, url: str, *, on_observation: ObservationCallback | None = None,
                 queue_size: int = 64) -> None:
        self.url = url
        self.on_observation = on_observation
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(4, queue_size))
        self.socket: Any = None
        self.receive_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.pending_finish: dict[tuple[str, int], asyncio.Future[None]] = {}
        self.pending_health: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_embed: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.dropped_frames = 0
        self.degraded = False
        self.last_error: str | None = None
        self.model_version = ""

    @property
    def connected(self) -> bool:
        return self.socket is not None

    def _mark_degraded(self, reason: str) -> None:
        """Record a protocol/transport fault while keeping the socket alive.

        The optional sidecar is fail-open.  A malformed result must therefore
        never be silently treated as a valid low score; marking the client
        degraded makes the semantic gate pass the current turn through and
        lets the next health/reconnect cycle recover the service.
        """

        self.degraded = True
        self.last_error = str(reason or "speaker worker protocol error")[:300]

    async def connect(self) -> None:
        if self.socket is not None:
            return
        try:
            import websockets  # type: ignore
            self.socket = await websockets.connect(self.url, open_timeout=2, ping_interval=20)
            self.receive_task = asyncio.create_task(self._receive_loop(), name="speaker-worker-receive")
            self.send_task = asyncio.create_task(self._send_loop(), name="speaker-worker-send")
            self.last_error = None
            self.degraded = False
        except Exception as exc:
            self.socket = None
            self.last_error = str(exc)[:300]
            raise SpeakerWorkerError(f"Speaker Worker 连接失败：{exc}") from exc

    async def _receive_loop(self) -> None:
        socket = self.socket
        if socket is None:
            return
        try:
            async for raw in socket:
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    self._mark_degraded("Speaker Worker 返回了无效 JSON")
                    continue
                if not isinstance(message, dict):
                    self._mark_degraded("Speaker Worker 返回了无效消息")
                    continue
                kind = message.get("type")
                if kind == "health":
                    request_id = message.get("request_id")
                    waiter = self.pending_health.get(request_id) if isinstance(request_id, str) else None
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                    continue
                if kind == "speaker.embed_result":
                    request_id = message.get("request_id")
                    waiter = self.pending_embed.get(request_id) if isinstance(request_id, str) else None
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                    continue
                if kind == "speaker.embedding":
                    embedding = message.get("embedding")
                    if not isinstance(embedding, list) or len(embedding) > 4096:
                        self._mark_degraded("Speaker Worker embedding 格式无效")
                        continue
                    try:
                        turn_id = str(message.get("turn_id") or "")
                        raw_generation = message.get("generation")
                        raw_window_start = message.get("window_started_sample")
                        raw_window_end = message.get("window_ended_sample")
                        if (isinstance(raw_generation, bool) or not isinstance(raw_generation, int)
                                or isinstance(raw_window_start, bool) or not isinstance(raw_window_start, int)
                                or isinstance(raw_window_end, bool) or not isinstance(raw_window_end, int)):
                            self._mark_degraded("Speaker Worker embedding 关联字段无效")
                            continue
                        generation = raw_generation
                        window_start = raw_window_start
                        window_end = raw_window_end
                        if (not turn_id or generation < 0 or window_start < 0
                                or window_end <= window_start):
                            self._mark_degraded("Speaker Worker embedding 窗口无效")
                            continue
                        speech_ms = message.get("speech_ms")
                        if (isinstance(speech_ms, bool) or not isinstance(speech_ms, int)
                                or speech_ms < 0):
                            self._mark_degraded("Speaker Worker embedding 语音时长无效")
                            continue
                        values = tuple(float(item) for item in embedding)
                        if any(not math.isfinite(item) for item in values):
                            self._mark_degraded("Speaker Worker embedding 包含无效数值")
                            continue
                        similarity = message.get("similarity")
                        if similarity is not None:
                            if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
                                self._mark_degraded("Speaker Worker similarity 格式无效")
                                continue
                            similarity = float(similarity)
                            if not math.isfinite(similarity):
                                self._mark_degraded("Speaker Worker similarity 包含无效数值")
                                continue
                            similarity = max(-1.0, min(1.0, similarity))
                        matched_template_id = message.get("matched_template_id")
                        if matched_template_id is not None and not isinstance(matched_template_id, str):
                            self._mark_degraded("Speaker Worker 模板标识无效")
                            continue
                        observation = SpeakerObservation(
                            turn_id=turn_id,
                            generation=generation,
                            window_started_sample=window_start,
                            window_ended_sample=window_end,
                            similarity=similarity,
                            matched_template_id=matched_template_id,
                            speech_ms=speech_ms,
                            quality=str(message.get("quality") or "unknown"),
                            model_version=str(message.get("model_version") or ""),
                            timestamp=str(message.get("timestamp") or ""),
                            embedding=values,
                        )
                    except (TypeError, ValueError, OverflowError):
                        self._mark_degraded("Speaker Worker embedding 解析失败")
                        continue
                    self.model_version = observation.model_version or self.model_version
                    if self.on_observation is not None:
                        await self.on_observation(observation)
                elif kind == "speaker.finished":
                    turn_id = message.get("turn_id")
                    generation = message.get("generation")
                    if (not isinstance(turn_id, str) or not turn_id.strip()
                            or isinstance(generation, bool) or not isinstance(generation, int)
                            or generation < 0):
                        continue
                    key = (turn_id, generation)
                    waiter = self.pending_finish.pop(key, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                elif kind == "error":
                    self.last_error = str(message.get("message") or "speaker worker error")[:300]
                    # Any worker-side protocol/model failure makes the
                    # current turn fail open.  Keep the degraded flag until
                    # an explicit reconnect/health check so a later finish
                    # cannot accidentally treat a partial result as a safe
                    # non-owner decision.
                    self.degraded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)[:300]
            logger.info("speaker.client.receive_failed error=%s", self.last_error)
        finally:
            if self.socket is socket:
                self.socket = None
                error = SpeakerWorkerError("Speaker Worker 连接已断开", code="SPEAKER_WORKER_DISCONNECTED")
                for waiter in self.pending_finish.values():
                    if not waiter.done():
                        waiter.set_exception(error)
                self.pending_finish.clear()
                for waiter in self.pending_health.values():
                    if not waiter.done():
                        waiter.set_exception(error)
                self.pending_health.clear()
                for waiter in self.pending_embed.values():
                    if not waiter.done():
                        waiter.set_exception(error)
                self.pending_embed.clear()

    async def _send_loop(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                socket = self.socket
                if socket is None:
                    continue
                async with self.send_lock:
                    await socket.send(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:300]
                logger.info("speaker.client.send_failed error=%s", self.last_error)
            finally:
                self.queue.task_done()

    async def _send_immediate(self, item: dict[str, Any]) -> None:
        socket = self.socket
        if socket is None:
            raise SpeakerWorkerError("Speaker Worker 尚未连接")
        async with self.send_lock:
            await socket.send(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

    async def health(self, timeout_s: float = 1.5) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        if self.socket is None:
            raise SpeakerWorkerError("Speaker Worker 尚未连接")
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_health[request_id] = waiter
        try:
            await self._send_immediate({"type": "health", "request_id": request_id})
            response = await asyncio.wait_for(waiter, timeout=max(0.05, float(timeout_s)))
            if response.get("status") != "READY" or response.get("model_loaded") is not True:
                raise SpeakerWorkerError("Speaker Worker 未就绪", code="SPEAKER_WORKER_NOT_READY")
            self.model_version = str(response.get("model_version") or self.model_version)
            return response
        except Exception as exc:
            raise SpeakerWorkerError(str(exc), code="SPEAKER_WORKER_HEALTH_FAILED") from exc
        finally:
            self.pending_health.pop(request_id, None)

    async def embed_pcm(self, pcm: bytes, *, timeout_ms: int = 2500) -> tuple[float, ...]:
        if self.socket is None:
            raise SpeakerWorkerError("Speaker Worker 尚未连接")
        request_id = uuid.uuid4().hex
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_embed[request_id] = waiter
        try:
            await self._send_immediate({
                "type": "embed", "request_id": request_id,
                "pcm_base64": base64.b64encode(pcm).decode("ascii"),
            })
            response = await asyncio.wait_for(waiter, timeout=max(0.05, timeout_ms / 1000))
            if response.get("quality") != "ready" or not isinstance(response.get("embedding"), list):
                raise SpeakerWorkerError(
                    f"录音质量不足：{response.get('quality') or 'unknown'}",
                    code="SPEAKER_AUDIO_QUALITY",
                )
            return tuple(float(item) for item in response["embedding"])
        except SpeakerWorkerError:
            raise
        except Exception as exc:
            raise SpeakerWorkerError(str(exc), code="SPEAKER_WORKER_EMBED_FAILED") from exc
        finally:
            self.pending_embed.pop(request_id, None)

    def submit_frame(self, *, turn_id: str, generation: int, pcm: bytes,
                     start_sample: int, end_sample: int) -> bool:
        if self.socket is None or not pcm:
            return False
        item = {
            "type": "audio.frame", "turn_id": turn_id, "generation": generation,
            "start_sample": int(start_sample), "end_sample": int(end_sample),
            "pcm_base64": base64.b64encode(pcm).decode("ascii"),
        }
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped_frames += 1
            self.degraded = True
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.queue.put_nowait(item)
            except asyncio.QueueEmpty:
                pass
            except asyncio.QueueFull:
                pass
            return False

    async def finish_turn(self, turn_id: str, generation: int, *, timeout_ms: int = 150) -> bool:
        if self.socket is None:
            return False
        loop = asyncio.get_running_loop()
        key = (turn_id, generation)
        waiter: asyncio.Future[None] = loop.create_future()
        self.pending_finish[key] = waiter
        # Keep the semantic gate bounded even if an embedding caller passes a
        # looser value by mistake.  The product contract is a hard 150 ms
        # ceiling from ASR final to routing decision.
        budget_ms = min(150, max(0, int(timeout_ms)))
        deadline = time.monotonic() + max(0.01, budget_ms / 1000)
        queue_drained = True
        finished = False
        finish_item = {"type": "turn.finish", "turn_id": turn_id, "generation": generation}
        try:
            # Preserve frame ordering: a direct finish command must not race
            # ahead of PCM still waiting in the bounded send queue.
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.wait_for(self.queue.join(), timeout=remaining)
                else:
                    raise asyncio.TimeoutError
            except asyncio.TimeoutError:
                queue_drained = False
            # Always release the worker-side turn buffer, even when a full
            # queue consumed the 150 ms budget.  The command may race with a
            # final frame in this degraded path, but fail-open routing is safer
            # than retaining stale generation data.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.queue.put_nowait(finish_item)
                except asyncio.QueueFull:
                    try:
                        self.queue.get_nowait()
                        self.queue.task_done()
                        self.queue.put_nowait(finish_item)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
                return False
            await asyncio.wait_for(
                self._send_immediate(finish_item),
                timeout=remaining,
            )
            remaining = deadline - time.monotonic()
            if queue_drained and remaining > 0:
                await asyncio.wait_for(waiter, timeout=remaining)
                finished = True
            return finished
        except Exception:
            return False
        finally:
            self.pending_finish.pop(key, None)

    async def close(self) -> None:
        for task in (self.send_task, self.receive_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(self.send_task, self.receive_task, return_exceptions=True)
        self.send_task = self.receive_task = None
        socket, self.socket = self.socket, None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass
        for waiter in self.pending_finish.values():
            if not waiter.done():
                waiter.cancel()
        self.pending_finish.clear()
        for waiter in self.pending_health.values():
            if not waiter.done():
                waiter.cancel()
        self.pending_health.clear()
        for waiter in self.pending_embed.values():
            if not waiter.done():
                waiter.cancel()
        self.pending_embed.clear()
