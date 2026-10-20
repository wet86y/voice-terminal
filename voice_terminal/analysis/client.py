import asyncio
import base64
import json
import os
import uuid
import time


class AudioReferenceRejected(RuntimeError):
    """共享引用在提交原生任务之前被拒绝，可安全回退原协议。"""


class AnalysisClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self._shared_peer = None

    async def health(self) -> dict:
        if os.environ.get("VOICE_APP_ANALYSIS_AVAILABLE") == "0":
            raise RuntimeError("应用未确认分析工作进程所有权或就绪状态")
        import websockets
        async with websockets.connect(self.url, open_timeout=2, close_timeout=0.2) as socket:
            await socket.send(json.dumps({"operation": "health"}))
            result = json.loads(await asyncio.wait_for(socket.recv(), 2))
            self._shared_peer = result.get("shared_audio") or False
            return result

    async def configure(self, enabled: dict[str, bool]) -> dict:
        """configure（模型切换）：只允许停止监听后的受管调用。"""
        import websockets
        async with websockets.connect(self.url, open_timeout=5, close_timeout=0.2) as socket:
            await socket.send(json.dumps({"operation": "configure", "enabled": enabled}))
            result = json.loads(await asyncio.wait_for(socket.recv(), 240))
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result

    async def analyze(self, operation: str, pcm: bytes, turn_id: str, generation_id: int, timeout_ms: int) -> dict:
        if os.environ.get("VOICE_APP_ANALYSIS_AVAILABLE") == "0":
            raise RuntimeError("应用未确认分析工作进程所有权或就绪状态")
        request_id = uuid.uuid4().hex
        from ..audio.shared import SharedAudio, compatible
        from ..resources import ResourcePolicy
        if ResourcePolicy.load().shared_audio and self._shared_peer is None:
            try:
                await self.health()
            except Exception:
                self._shared_peer = False  # 旧客户端、外部进程及探测失败沿用正文传输。
        shared = None
        try:
            if compatible(self._shared_peer):
                try:
                    shared = SharedAudio(pcm, self._shared_peer, request_id)
                except (OSError, ValueError):
                    pass
            started = time.monotonic()
            try:
                return await self._analyze(operation, pcm, turn_id, generation_id, timeout_ms, request_id, shared)
            except AudioReferenceRejected:
                if shared is None:
                    raise
                self._shared_peer = None
                remaining_ms = timeout_ms - (time.monotonic() - started) * 1000
                if remaining_ms <= 0:
                    raise TimeoutError
                return await self._analyze(operation, pcm, turn_id, generation_id, remaining_ms, request_id, None)
        finally:
            if shared is not None:
                shared.close()

    async def _analyze(self, operation, pcm, turn_id, generation_id, timeout_ms, request_id, shared):
        import websockets
        async with websockets.connect(self.url, open_timeout=timeout_ms / 1000, close_timeout=0.1,
                                      max_size=1024 * 1024) as socket:
            try:
                async with asyncio.timeout(timeout_ms / 1000):
                    audio = {"audio_ref": shared.reference} if shared else {"pcm_base64": base64.b64encode(pcm).decode("ascii")}
                    await socket.send(json.dumps({"operation": operation, "request_id": request_id,
                                                  "turn_id": turn_id, "generation_id": generation_id, **audio}))
                    response = json.loads(await socket.recv())
                    if response.get("error"):
                        if (shared is not None and response.get("code") == "AUDIO_REF_REJECTED"
                                and response.get("request_id") == request_id):
                            raise AudioReferenceRejected(response["error"])
                        raise RuntimeError(response["error"])
                    if (response.get("request_id"), response.get("turn_id"), response.get("generation_id")) != (request_id, turn_id, generation_id):
                        raise RuntimeError("analysis identity mismatch")
                    return response
            except (asyncio.CancelledError, TimeoutError):
                # asyncio cancellation alone does not interrupt ONNX Runtime.
                try:
                    await asyncio.wait_for(socket.send(json.dumps({
                        "operation": "cancel", "request_id": request_id,
                    })), 0.1)
                    await asyncio.wait_for(socket.recv(), 0.2)
                except Exception:
                    pass
                raise
