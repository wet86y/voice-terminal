"""真实本机 WebSocket（双向连接）验证协商、过期回退及取消租约。"""
import asyncio
import os
import socket
import threading
import time

import pytest

from voice_terminal.analysis.client import AnalysisClient
from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
from voice_terminal.resources import registry


@pytest.mark.parametrize("cancel", [False, True])
def test_negotiated_shared_request_retries_only_before_native_start_and_survives_cancel(tmp_path, monkeypatch, cancel):
    if os.name != "nt":
        pytest.skip("Windows（操作系统）共享音频协议")
    import uvicorn
    path = tmp_path / "policy.json"; path.write_text('{"shared_audio":true}')
    for key, value in {"VOICE_RESOURCE_POLICY": str(path), "VOICE_APP_INSTANCE_ID": "integration-test",
                       "VOICE_APP_INTERNAL_TOKEN": "integration-secret", "VOICE_APP_SERVICE_GENERATION": "receiver"}.items():
        monkeypatch.setenv(key, value)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, readings = [], []
    pcm = b"\x01\0" * 16000
    def model(value):
        calls.append(1); entered.set()
        if cancel:
            assert release.wait(5)
        readings.append(bytes(value)); finished.set()
        return {"complete": True, "text": "固定响应"}
    lane = AnalysisLane(model)
    listener = socket.socket(); listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_analysis_app({"refine": lane}), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    before = registry.snapshot()["active_leases"]
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        async def scenario():
            client = AnalysisClient(f"ws://127.0.0.1:{port}/analysis")
            assert (await client.health())["shared_audio"]
            if not cancel:
                client._shared_peer = {**client._shared_peer, "generation": "expired-generation"}
                result = await client.analyze("refine", pcm, "turn", 1, 5000)
                assert result["result"]["text"] == "固定响应"
                assert client._shared_peer is None
            else:
                task = asyncio.create_task(client.analyze("refine", pcm, "turn", 1, 5000))
                deadline = time.monotonic() + 3
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.005)
                assert entered.is_set()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert registry.snapshot()["active_leases"] > before
                release.set()
                deadline = time.monotonic() + 3
                while (not finished.is_set() or registry.snapshot()["active_leases"] != before) and time.monotonic() < deadline:
                    await asyncio.sleep(.005)
            assert calls == [1]  # 引用过期回退没有重复推理；取消也没有提前释放读取页面。
            assert readings == [pcm]
            assert registry.snapshot()["active_leases"] == before
        asyncio.run(scenario())
    finally:
        release.set(); server.should_exit = True; thread.join(5); listener.close()
        assert not thread.is_alive()
