from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Callable

from .protocol import decode_request


class AnalysisLane:
    """No backlog: cancellation does not release the slot of a running native call."""

    def __init__(
        self,
        model: Callable | None = None,
        error: str | None = None,
        *,
        model_name: str | None = None,
    ) -> None:
        self.model, self.error, self.model_name = model, error, model_name
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-analysis")
        self.active: asyncio.Future | None = None

    def health_payload(self) -> dict:
        return {
            "status": "READY" if self.model else "DEGRADED",
            "model": self.model_name,
            "model_loaded": self.model is not None,
            "error": self.error,
        }

    async def run(self, pcm: bytes) -> dict:
        if self.model is None:
            raise RuntimeError(self.error or "model unavailable")
        if self.active is not None and not self.active.done():
            raise RuntimeError("analysis busy")
        self.active = asyncio.get_running_loop().run_in_executor(self.executor, self.model, pcm)
        # Retrieve errors even if the requesting connection has gone away.
        self.active.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return await asyncio.shield(self.active)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def create_analysis_app(lanes: dict[str, AnalysisLane]):
    from fastapi import FastAPI, WebSocket
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        yield
        for lane in lanes.values():
            lane.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {name: lane.health_payload() for name, lane in lanes.items()}

    # FastAPI must resolve this annotation in the module namespace.
    globals()["WebSocket"] = WebSocket

    @app.websocket("/analysis")
    async def analyze(socket: WebSocket):
        await socket.accept()
        request = {}
        try:
            request = await asyncio.wait_for(socket.receive_json(), 5)
            if request.get("operation") == "health":
                await socket.send_json(await health())
                return
            pcm = decode_request(request)
            operation = request["operation"]
            started = time.monotonic()
            result = await lanes[operation].run(pcm)
            await socket.send_json({"request_id": request["request_id"], "turn_id": request["turn_id"],
                                    "generation_id": request["generation_id"], "result": result,
                                    "elapsed_ms": (time.monotonic() - started) * 1000})
        except Exception as exc:
            try:
                await socket.send_json({"request_id": request.get("request_id"), "error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                await socket.close()
            except Exception:
                pass

    return app
