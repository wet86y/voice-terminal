"""启动优化回归：超时预算、轻量探测、阶段日志和声纹应用层就绪。"""
from __future__ import annotations

import asyncio
import builtins
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from voice_terminal import compute
from voice_terminal.application.state import ApplicationState
from voice_terminal.application.supervisor import ApplicationHost
from voice_terminal.core.state import RuntimeState
from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
from voice_terminal.startup_logging import startup_stage
from voice_terminal.speaker.client import SpeakerWorkerClient


def test_startup_stages_write_to_stderr_and_preserve_json_stdout(capsys, monkeypatch):
    monkeypatch.setenv("VOICE_APP_INTERNAL_TOKEN", "must-not-be-logged")
    with startup_stage("检查演示组件"):
        print('{"result":true}')
    captured = capsys.readouterr()
    assert captured.out == '{"result":true}\n'
    events = [json.loads(line.split(" startup.progress ", 1)[1]) for line in captured.err.splitlines()]
    assert [row["event"] for row in events] == ["started", "completed"]
    assert events[1]["elapsed_ms"] >= 0
    assert "must-not-be-logged" not in captured.err
    with pytest.raises(RuntimeError):
        with startup_stage("失败阶段"):
            raise RuntimeError("演示错误")
    assert '"event": "failed"' in capsys.readouterr().err


def test_onnx_probe_never_attempts_to_import_torch_or_sherpa(monkeypatch):
    original = builtins.__import__
    attempted = []
    def guarded(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"torch", "torchaudio", "sherpa_onnx"}:
            attempted.append(name)
            raise AssertionError("声纹探测不能导入无关框架")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(compute, "_onnx_cuda_probe", lambda _: True)
    monkeypatch.setattr(compute.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="GPU, 4096, 8192"))
    result = compute._backend_hardware_status(onnx_only=True)
    assert not attempted
    assert result["models"]["wespeaker"]["gpu_available"]
    assert not result["backends"]["pytorch_cuda"]


def test_speaker_auto_uses_separate_onnx_capability_cache(monkeypatch):
    hardware = {"models": {"wespeaker": {"gpu_available": True}},
                "gpu": {"free_bytes": 2 * 1024**3}, "backends": {}, "system_memory": {}}
    monkeypatch.setenv("VOICE_RESOURCE_ROLE", "speaker")
    monkeypatch.setenv("VOICE_GPU_PROBE", "1")
    monkeypatch.delenv("VOICE_GPU_FALLBACK", raising=False)
    monkeypatch.setattr(compute, "_onnx_capabilities", None)
    monkeypatch.setattr(compute, "_capabilities", {"models": {}})
    probe = Mock(return_value=hardware)
    monkeypatch.setattr(compute, "_backend_hardware_status", probe)
    assert compute.device_candidates("auto", "wespeaker", onnx_only=True) == ["cuda", "cpu"]
    assert compute.device_candidates("auto", "wespeaker", onnx_only=True) == ["cuda", "cpu"]
    probe.assert_called_once_with(onnx_only=True)
    assert compute._capabilities == {"models": {}}


def test_explicit_cuda_does_not_probe_other_frameworks(monkeypatch):
    monkeypatch.delenv("VOICE_GPU_FALLBACK", raising=False)
    with patch.object(compute, "hardware_status", side_effect=AssertionError("实际加载器验证显式设备")):
        assert compute.device_candidates("cuda", "wespeaker", onnx_only=True) == ["cuda", "cpu"]


def test_dsh_inventory_is_reused_during_startup_and_rechecked_on_retry(tmp_path):
    app = ApplicationHost.__new__(ApplicationHost)
    app.root = tmp_path / "app"
    app.paths = SimpleNamespace(install_root=tmp_path)
    app.manifest = {"dsh_enabled": True, "resource_requirements": {}, "model_dir": str(tmp_path / "models")}
    app.no_worker = app.no_analysis = False
    with patch("voice_terminal.application.supervisor.inspect_model_resources", return_value={}), \
         patch("voice_terminal.local_layout.read_portable", return_value={"dsh": "runtimes/dsh"}), \
         patch("voice_terminal.application.supervisor.inspect_dsh_runtime", side_effect=[None, "组件文件缺失"]) as inspect:
        assert app._refresh_resource_issues() == {}
        assert app._refresh_resource_issues() == {}
        assert inspect.call_count == 1
        assert app._refresh_resource_issues(recheck_dsh=True) == {"dsh": "组件文件缺失"}
        assert inspect.call_count == 2


def bootstrap_stub(tmp_path, process):
    app = ApplicationHost.__new__(ApplicationHost)
    app.paths = SimpleNamespace(logs=tmp_path, separated=True)
    app.root = Path.cwd()
    app.config = "config/local.yaml"
    app.state = ApplicationState()
    app.resolve_python = Mock(return_value="python.exe")
    app.stop_group = Mock()
    app._refresh_resource_issues = Mock(return_value={})
    app.resource_issues = {}
    app.headless = True
    app.frontend_error = ""
    app.dev = False
    def spawn(*_):
        manifest = {"asr_url": "ws://127.0.0.1:8765/asr", "analysis_url": "ws://127.0.0.1:8767/analysis",
                    "voice_host": "127.0.0.1", "voice_port": 8766, "resource_issues": {}}
        (tmp_path / "application-bootstrap.log").write_text(
            'now startup.progress {"stage":"核验代理组件"}\n' + json.dumps(manifest) + '\n', encoding="utf-8")
        return process
    app.spawn = Mock(side_effect=spawn)
    return app


def test_bootstrap_can_complete_after_old_thirty_second_limit(tmp_path):
    async def scenario():
        process = SimpleNamespace(pid=10, poll=Mock(side_effect=[None, None, 0, 0]))
        app = bootstrap_stub(tmp_path, process)
        with patch("voice_terminal.paths.python_command", return_value=["python.exe"]), \
             patch("voice_terminal.application.supervisor.time.monotonic", side_effect=[0, 31, 32, 34]), \
             patch("voice_terminal.application.supervisor.asyncio.sleep", new=AsyncMock()):
            await app.setup()
        assert app._dsh_resource_checked
        app.stop_group.assert_called_once_with("bootstrap")
        assert next(c for c in app.state.snapshot()["components"] if c["id"] == "setup")["state"] == "ready"
    asyncio.run(scenario())


def test_bootstrap_timeout_reports_the_last_phase(tmp_path):
    async def scenario():
        app = bootstrap_stub(tmp_path, SimpleNamespace(pid=10, poll=lambda: None))
        with patch("voice_terminal.paths.python_command", return_value=["python.exe"]), \
             patch("voice_terminal.application.supervisor.time.monotonic", side_effect=[0, 181]):
            with pytest.raises(RuntimeError, match="180 秒.*核验代理组件"):
                await app.setup()
    asyncio.run(scenario())


def test_managed_speaker_waits_for_health_before_creating_business_client(tmp_path, monkeypatch):
    async def scenario():
        model = tmp_path / "model.onnx"
        model.write_bytes(b"model")
        runtime = SpeakerRuntimeMixin()
        runtime._speaker_worker_lock = asyncio.Lock()
        runtime.speaker = None
        runtime.state = RuntimeState()
        runtime.config = SimpleNamespace(speaker_model_path=model, speaker_worker_url="ws://127.0.0.1:1234/speaker")
        runtime._speaker_worker_process = SimpleNamespace(pid=10, returncode=None)
        runtime._speaker_worker_url_for_process = AsyncMock(return_value=runtime.config.speaker_worker_url)
        runtime._connect_speaker_worker = AsyncMock(return_value=(True, None))
        runtime._release_speaker_engine = Mock()
        probe = AsyncMock(side_effect=[(False, {"error": "正在加载"}), (True, {"status": "READY"})])
        with patch("voice_terminal.server.speaker_runtime.probe_speaker_worker", probe), \
             patch("voice_terminal.server.speaker_runtime.asyncio.sleep", new=AsyncMock()):
            assert await runtime.ensure_speaker_worker()
        assert probe.await_count == 2
        runtime._connect_speaker_worker.assert_awaited_once()
    monkeypatch.setenv("VOICE_APP_ORIGIN", "http://127.0.0.1:5173")
    asyncio.run(scenario())


def test_cancelled_speaker_handshake_closes_the_partial_client():
    async def scenario():
        runtime = SpeakerRuntimeMixin()
        runtime.config = SimpleNamespace(speaker_worker_url="ws://127.0.0.1:1234/speaker")
        entered = asyncio.Event()
        async def connect():
            entered.set()
            await asyncio.Future()
        client = SimpleNamespace(connect=connect, close=AsyncMock())
        with patch("voice_terminal.server.speaker_runtime.SpeakerWorkerClient", return_value=client):
            task = asyncio.create_task(runtime._connect_speaker_worker())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        client.close.assert_awaited_once()
    asyncio.run(scenario())


def test_settings_import_does_not_load_the_shadow_runtime():
    result = subprocess.run([sys.executable, "-B", "-c",
        "import sys; from voice_terminal.server.product import ProductSettingsStore; "
        "assert 'voice_terminal.shadow_agent' not in sys.modules"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_speaker_parent_keeps_worker_export_lazy():
    result = subprocess.run([sys.executable, "-B", "-c",
        "import sys; import voice_terminal.speaker; "
        "assert 'voice_terminal.speaker.worker' not in sys.modules; "
        "from voice_terminal.speaker import SpeakerWorkerEngine; "
        "assert SpeakerWorkerEngine.__name__ == 'SpeakerWorkerEngine'"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_speaker_handshake_can_finish_after_old_two_second_limit():
    async def scenario():
        import websockets
        async def delayed_handshake(*_):
            await asyncio.sleep(2.1)
        async def handler(socket, path=None):
            async for raw in socket:
                request = json.loads(raw)
                await socket.send(json.dumps({"type": "health", "request_id": request["request_id"],
                                               "status": "READY", "model_loaded": True}))
        async with websockets.serve(handler, "127.0.0.1", 0, process_request=delayed_handshake) as server:
            port = server.sockets[0].getsockname()[1]
            client = SpeakerWorkerClient(f"ws://127.0.0.1:{port}/speaker")
            try:
                await client.connect()
                await client.health(timeout_s=1)
                assert client.connected and not client.degraded
            finally:
                await client.close()
    asyncio.run(scenario())
