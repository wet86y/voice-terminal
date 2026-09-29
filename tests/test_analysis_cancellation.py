import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from voice_terminal.analysis.client import AnalysisClient
from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app


class TerminableModel:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()

    def new_run_options(self):
        return SimpleNamespace(terminate=False)

    def __call__(self, pcm, options):
        self.started.set()
        while not self.release.is_set():
            if options.terminate:
                self.cancelled.set()
                raise RuntimeError("native inference terminated")
            time.sleep(0.005)
        return {"probability": 0.8}


def request(request_id):
    return {
        "operation": "endpoint", "request_id": request_id,
        "turn_id": "turn-a", "generation_id": 0,
        "pcm_base64": base64.b64encode(b"\0\0" * 320).decode(),
    }


def test_websocket_cancel_releases_native_lane_and_accepts_next_pause():
    model = TerminableModel()
    lane = AnalysisLane(model)
    with TestClient(create_analysis_app({"endpoint": lane})) as client:
        with client.websocket_connect("/analysis") as first:
            first.send_json(request("first"))
            assert model.started.wait(2)
            with client.websocket_connect("/analysis") as busy:
                busy.send_json(request("busy"))
                assert busy.receive_json()["error"] == "analysis busy"
            first.send_json({"operation": "cancel", "request_id": "first"})
            assert first.receive_json() == {"request_id": "first", "cancelled": True}
            assert model.cancelled.is_set()

        model.release.set()
        with client.websocket_connect("/analysis") as next_pause:
            next_pause.send_json(request("next"))
            assert next_pause.receive_json()["result"] == {"probability": 0.8}


def test_cancel_that_cannot_stop_native_work_keeps_lane_busy_until_completion():
    started = threading.Event()
    release = threading.Event()

    def blocking_model(_pcm):
        started.set()
        release.wait(2)
        return {"probability": 0.2}

    async def scenario():
        lane = AnalysisLane(blocking_model)
        try:
            first = asyncio.create_task(lane.run(b"\0\0", "first"))
            assert await asyncio.to_thread(started.wait, 2)
            assert lane.cancel("first")
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(RuntimeError, match="analysis busy"):
                await lane.run(b"\0\0", "second")
            release.set()
            for _ in range(100):
                if lane.active is None:
                    break
                await asyncio.sleep(0.01)
            assert lane.active is None
            assert await lane.run(b"\0\0", "next") == {"probability": 0.2}
        finally:
            release.set()
            lane.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["timeout", "speech_resume"])
def test_client_interruption_sends_cancel_on_same_socket(monkeypatch, interrupt):
    import websockets

    class Socket:
        def __init__(self):
            self.sent = []
            self.receives = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def recv(self):
            self.receives += 1
            if self.receives == 1:
                await asyncio.Event().wait()
            return json.dumps({"request_id": self.sent[0]["request_id"], "cancelled": True})

    socket = Socket()
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: socket)

    async def scenario():
        client = AnalysisClient("ws://unused")
        if interrupt == "timeout":
            with pytest.raises(TimeoutError):
                await client.analyze("endpoint", b"\0\0", "turn-a", 0, 20)
        else:
            task = asyncio.create_task(client.analyze("endpoint", b"\0\0", "turn-a", 0, 1000))
            while not socket.sent:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert socket.sent[1] == {"operation": "cancel", "request_id": socket.sent[0]["request_id"]}
