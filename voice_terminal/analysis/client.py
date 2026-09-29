import asyncio
import base64
import json
import uuid


class AnalysisClient:
    def __init__(self, url: str) -> None:
        self.url = url

    async def health(self) -> dict:
        import websockets
        async with websockets.connect(self.url, open_timeout=2, close_timeout=0.2) as socket:
            await socket.send(json.dumps({"operation": "health"}))
            return json.loads(await asyncio.wait_for(socket.recv(), 2))

    async def analyze(self, operation: str, pcm: bytes, turn_id: str, generation_id: int, timeout_ms: int) -> dict:
        import websockets
        request_id = uuid.uuid4().hex
        async with websockets.connect(self.url, open_timeout=timeout_ms / 1000, close_timeout=0.1,
                                      max_size=1024 * 1024) as socket:
            try:
                async with asyncio.timeout(timeout_ms / 1000):
                    await socket.send(json.dumps({"operation": operation, "request_id": request_id,
                                                  "turn_id": turn_id, "generation_id": generation_id,
                                                  "pcm_base64": base64.b64encode(pcm).decode("ascii")}))
                    response = json.loads(await socket.recv())
                    if response.get("error"):
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
