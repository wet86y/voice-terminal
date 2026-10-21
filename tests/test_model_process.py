"""按需模型进程的真实退出、重复关闭和启动失败回收。"""
import sys
from pathlib import Path

import psutil
import pytest

from voice_terminal.model_process import ModelProcess


@pytest.fixture
def child_command(monkeypatch, tmp_path):
    def configure(*, failure=False):
        source = f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1])!r})
from voice_terminal import model_process
class Model:
    device = "cpu"
    warmed = False
    def __init__(self):
        if {failure!r}:
            raise RuntimeError("模拟加载失败")
        self.weight = bytearray(16 * 1024 * 1024)
    def warmup(self):
        self.warmed = True
    def __call__(self, pcm):
        return {{"bytes": len(pcm)}}
model_process._create_model = lambda *args: Model()
sys.argv = ["model_process", "emotion", {str(tmp_path)!r}]
model_process.main()
'''
        monkeypatch.setattr("voice_terminal.paths.python_command", lambda *args, **kwargs:
                            [sys.executable, "-B", "-X", "utf8", "-c", source])
    return configure


def test_optional_model_process_exits_and_can_be_enabled_again(child_command, tmp_path):
    child_command()
    for _attempt in range(2):
        model = ModelProcess("emotion", tmp_path)
        process = psutil.Process(model.pid)
        try:
            assert model.device == "cpu" and model.warmed
            assert process.is_running()
            assert model(b"\0\0" * 160) == {"bytes": 320}
            assert model.resource_status["pid"] == model.pid
            assert model.resource_status["created_at"] == process.create_time()
            assert not model.tensor_cuda_build
        finally:
            model.close()
        assert not process.is_running()
        model.close()
        assert model.resource_status is None


def test_failed_model_start_leaves_no_child_process(child_command, tmp_path):
    child_command(failure=True)
    before = {child.pid for child in psutil.Process().children()}
    with pytest.raises(RuntimeError, match="模拟加载失败"):
        ModelProcess("emotion", tmp_path)
    assert {child.pid for child in psutil.Process().children()} <= before


def test_host_cpu_fallback_is_not_bypassed_by_optional_gpu_model(child_command, tmp_path, monkeypatch):
    child_command()
    monkeypatch.setenv("VOICE_GPU_FALLBACK", "1")
    model = ModelProcess("emotion", tmp_path, device="cuda")
    try:
        assert model.device == "cpu" and model.requested_device == "cuda"
        assert "宿主" in model.load_error
        assert model(b"\0\0") == {"bytes": 2}
    finally:
        model.close()


def test_analysis_toggle_exits_model_process_and_resets_footprint(child_command, tmp_path):
    from starlette.testclient import TestClient
    from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app

    child_command()
    lane = AnalysisLane(error="disabled")
    factory = lambda: ModelProcess("emotion", tmp_path)
    with TestClient(create_analysis_app({"emotion": lane}, {"emotion": factory})) as client:
        def configure(enabled):
            with client.websocket_connect("/analysis") as socket:
                socket.send_json({"operation": "configure", "enabled": {"emotion": enabled}})
                return socket.receive_json()["emotion"]
        assert configure(False)["status"] == "DISABLED"
        ready = configure(True)
        assert ready["status"] == "READY"
        assert ready["resource_status"]["pid"] == lane.model.pid
        assert ready["resource_footprint"]["pid"] == lane.model.pid
        process = psutil.Process(lane.model.pid)
        status = configure(False)
        assert status["status"] == "DISABLED" and not status["model_loaded"]
        assert status["resource_footprint"] is None
        assert not process.is_running()
        assert configure(True)["status"] == "READY"
        assert lane.model.pid != process.pid
        restarted = psutil.Process(lane.model.pid)
    assert not restarted.is_running()


@pytest.mark.asyncio
async def test_dual_pass_disable_waits_for_process_exit_and_can_reload(child_command, tmp_path):
    from voice_terminal.asr.dual_pass import DualPassLane

    child_command()
    lane = DualPassLane(tmp_path)
    try:
        await lane.configure(True, "cpu", strict=True)
        process = psutil.Process(lane.model.pid)
        assert (await lane.infer(b"\0\0" * 160))["bytes"] == 320
        await lane.configure(False, "cpu", strict=True)
        assert not process.is_running()
        assert lane.status()["status"] == "DISABLED"
        assert lane.resource_footprint is None
        await lane.configure(True, "cpu", strict=True)
        restarted = psutil.Process(lane.model.pid)
    finally:
        lane.close()
    assert not restarted.is_running()


def test_analysis_child_crash_is_degraded_and_can_be_reloaded(child_command, tmp_path):
    from starlette.testclient import TestClient
    from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app

    child_command()
    lane = AnalysisLane(error="disabled")
    factory = lambda: ModelProcess("emotion", tmp_path)
    with TestClient(create_analysis_app({"emotion": lane}, {"emotion": factory})) as client:
        def enable():
            with client.websocket_connect("/analysis") as socket:
                socket.send_json({"operation": "configure", "enabled": {"emotion": True}})
                return socket.receive_json()["emotion"]
        assert enable()["status"] == "READY"
        crashed_pid = lane.model.pid
        lane.model._process.kill()
        lane.model._process.wait(timeout=5)
        status = client.get("/health").json()["emotion"]
        assert status["status"] == "DEGRADED" and not status["model_loaded"]
        assert status["resource_status"] is None and status["resource_footprint"] is None
        assert "退出码" in status["error"]
        assert enable()["status"] == "READY" and lane.model.pid != crashed_pid
        restarted = psutil.Process(lane.model.pid)
    assert not restarted.is_running()


@pytest.mark.asyncio
async def test_dual_pass_child_crash_is_degraded_and_can_be_reloaded(child_command, tmp_path):
    from voice_terminal.asr.dual_pass import DualPassLane

    child_command()
    lane = DualPassLane(tmp_path)
    try:
        await lane.configure(True, "cpu", strict=True)
        crashed_pid = lane.model.pid
        lane.model._process.kill()
        lane.model._process.wait(timeout=5)
        status = lane.status()
        assert status["status"] == "DEGRADED" and not status["model_loaded"]
        assert status["resource_status"] is None and status["resource_footprint"] is None
        assert "退出码" in status["error"]
        await lane.configure(True, "cpu", strict=True)
        assert lane.status()["status"] == "READY" and lane.model.pid != crashed_pid
    finally:
        lane.close()
