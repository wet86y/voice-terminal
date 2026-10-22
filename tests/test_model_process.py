"""按需模型进程的真实退出、重复关闭和启动失败回收。"""
import sys
from pathlib import Path

import psutil
import pytest

from voice_terminal.model_process import ModelProcess


@pytest.fixture
def child_command(monkeypatch, tmp_path):
    def configure(*, failure=False, kind="emotion"):
        source = f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).parents[1])!r})
from voice_terminal import model_process
class Model:
    device = "cpu"
    requested_device = "cpu"
    runtime = "test-runtime"
    model_file = "test-model"
    warmed = False
    def __init__(self):
        if {failure!r}:
            raise RuntimeError("模拟加载失败")
        self.cache_by_turn = {{}}
        self.weight = bytearray(16 * 1024 * 1024)
    def warmup(self):
        self.warmed = True
    def __call__(self, pcm):
        return {{"bytes": len(pcm)}}
    def infer(self, turn_id, pcm, is_final):
        cache = self.cache_by_turn.setdefault(turn_id, {{}})
        cache["calls"] = cache.get("calls", 0) + 1
        result = cache["calls"]
        if is_final: self.cache_by_turn.pop(turn_id, None)
        return result
    def clear_turn(self, turn_id): self.cache_by_turn.pop(turn_id, None)
model_process._create_model = lambda *args: Model()
sys.argv = ["model_process", {kind!r}, {str(tmp_path)!r}]
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


def test_streaming_model_child_keeps_turn_caches_separate_and_clears_final_turn(child_command, tmp_path):
    child_command(kind="streaming")
    model = ModelProcess("streaming", tmp_path)
    try:
        assert model.warmed and model.device == "cpu"
        assert model.infer_stream("turn-a", b"\0\0", False) == 1
        assert model.infer_stream("turn-a", b"\0\0", False) == 2
        assert model.infer_stream("turn-b", b"\0\0", False) == 1
        assert model.infer_stream("turn-a", b"\0\0", True) == 3
        assert model.infer_stream("turn-a", b"\0\0", False) == 1
        model.clear_turn("turn-b")
    finally:
        process = psutil.Process(model.pid)
        model.close()
    assert not process.is_running()


def test_cpu_parent_can_route_streaming_child_to_registered_gpu_interpreter(child_command, tmp_path, monkeypatch):
    """父进程选择处理器依赖后，识别子进程仍按自身设备选择显卡解释器。"""
    from voice_terminal import paths
    child_command(kind="streaming")
    selected = []
    original_command = paths.python_command

    def command(executable, *args, **kwargs):
        selected.append(executable)
        return original_command(executable, *args, **kwargs)

    def choose(_install, _data, role, cpu_python, devices):
        assert role == "asr" and devices == {"paraformer": "cuda"}
        assert cpu_python == "cpu-python"
        return "gpu-python"

    monkeypatch.setenv("VOICE_CPU_PYTHON", "cpu-python")
    monkeypatch.setenv("VOICE_TENSOR_DEVICE", "cpu")
    monkeypatch.delenv("VOICE_GPU_FALLBACK", raising=False)
    monkeypatch.setattr(paths, "python_command", command)
    monkeypatch.setattr("voice_terminal.gpu_environment.worker_python", choose)
    model = ModelProcess("streaming", tmp_path, device="cuda")
    process = psutil.Process(model.pid)
    try:
        assert selected == ["gpu-python"]
        assert model.infer_stream("child-routing", b"\0\0", True) == 1
    finally:
        model.close()
    assert not process.is_running()


def test_analysis_toggle_exits_model_process_and_resets_footprint(child_command, tmp_path):
    from starlette.testclient import TestClient
    from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app

    child_command()
    lane = AnalysisLane(error="disabled")
    factory = lambda device=None, **_options: ModelProcess("emotion", tmp_path, device=device or "cpu")
    with TestClient(create_analysis_app({"emotion": lane}, {"emotion": factory})) as client:
        def configure(enabled, *, devices=None, allow_cuda_fallback=None):
            with client.websocket_connect("/analysis") as socket:
                socket.send_json({"operation": "configure", "enabled": {"emotion": enabled},
                                  "devices": devices or {},
                                  "allow_cuda_fallback": allow_cuda_fallback or {}})
                return socket.receive_json()["emotion"]
        assert configure(False)["status"] == "DISABLED"
        ready = configure(True)
        assert ready["status"] == "READY"
        assert ready["resource_status"]["pid"] == lane.model.pid
        assert ready["resource_footprint"]["pid"] == lane.model.pid
        process = psutil.Process(lane.model.pid)
        status = configure(True, devices={"emotion": "cuda"},
                           allow_cuda_fallback={"emotion": True})
        assert status["status"] == "READY"
        assert status["requested_device"] == "cuda" and status["device"] == "cpu"
        assert lane.device_preference == "cuda"
        assert not process.is_running()
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
    factory = lambda device=None: ModelProcess("emotion", tmp_path, device=device or "cpu")
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
