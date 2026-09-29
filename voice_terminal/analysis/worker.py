from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Callable

from .protocol import decode_request
from ..compute import process_memory_bytes


class AnalysisLane:
    """No backlog: keep the slot until a native call actually exits."""

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
        self.active_request_id: str | None = None
        self.active_run_options = None

    def health_payload(self) -> dict:
        model = self.model
        return {
            "status": "DISABLED" if self.error == "disabled" else "READY" if self.model else "DEGRADED",
            "model": self.model_name,
            "model_loaded": self.model is not None,
            "requested_device": getattr(model, "requested_device", None),
            "device": getattr(model, "device", None),
            "internal_vad_device": getattr(model, "internal_vad_device", None),
            "load_error": getattr(model, "load_error", None),
            "process_memory_rss_bytes": process_memory_bytes(),
            "error": self.error,
        }

    async def run(self, pcm: bytes, request_id: str) -> dict:
        if self.model is None:
            raise RuntimeError(self.error or "model unavailable")
        if self.active is not None and not self.active.done():
            raise RuntimeError("analysis busy")
        factory = getattr(self.model, "new_run_options", None)
        run_options = factory() if callable(factory) else None
        self.active_request_id = request_id
        self.active_run_options = run_options
        if run_options is None:
            self.active = asyncio.get_running_loop().run_in_executor(self.executor, self.model, pcm)
        else:
            self.active = asyncio.get_running_loop().run_in_executor(self.executor, self.model, pcm, run_options)
        active = self.active
        # Retrieve errors even if the requesting connection has gone away.
        def finished(f):
            if not f.cancelled():
                f.exception()
            if self.active is f:
                self.active = None
                self.active_request_id = None
                self.active_run_options = None
        active.add_done_callback(finished)
        return await asyncio.shield(active)

    def cancel(self, request_id: str) -> bool:
        if request_id != self.active_request_id or self.active is None or self.active.done():
            return False
        if self.active_run_options is not None:
            self.active_run_options.terminate = True
        return True

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
        run_task = receive_task = None
        lane = None
        try:
            request = await asyncio.wait_for(socket.receive_json(), 5)
            if request.get("operation") == "health":
                await socket.send_json(await health())
                return
            pcm = decode_request(request)
            operation = request["operation"]
            lane = lanes[operation]
            started = time.monotonic()
            run_task = asyncio.create_task(lane.run(pcm, request["request_id"]))
            receive_task = asyncio.create_task(socket.receive_json())
            done, _ = await asyncio.wait({run_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
            if run_task in done:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
                result = await run_task
                await socket.send_json({"request_id": request["request_id"], "turn_id": request["turn_id"],
                                        "generation_id": request["generation_id"], "result": result,
                                        "elapsed_ms": (time.monotonic() - started) * 1000})
                return
            try:
                control = receive_task.result()
            except Exception:
                # A disconnected client must not hold native endpoint inference.
                lane.cancel(request["request_id"])
                return
            if control.get("operation") != "cancel" or control.get("request_id") != request["request_id"]:
                raise ValueError("invalid analysis cancellation")
            lane.cancel(request["request_id"])
            await asyncio.gather(run_task, return_exceptions=True)
            await socket.send_json({"request_id": request["request_id"], "cancelled": True})
        except Exception as exc:
            try:
                await socket.send_json({"request_id": request.get("request_id"), "error": str(exc)})
            except Exception:
                pass
        finally:
            if lane is not None and request.get("request_id"):
                lane.cancel(request["request_id"])
            if run_task is not None and not run_task.done():
                run_task.cancel()
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
            pending = [task for task in (run_task, receive_task) if task is not None]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                await socket.close()
            except Exception:
                pass

    return app
