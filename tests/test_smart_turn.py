"""SmartTurn（语音结束判断）真实设备选择、预热失败与取消边界。"""
from pathlib import Path
import asyncio
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voice_terminal.analysis.smart_turn import SmartTurn


def backend(monkeypatch, tmp_path, *, available=True, silent_fallback=False, warmup_failure=False, gpu_file=True):
    (tmp_path / "smart-turn-v3.2-cpu.onnx").touch()
    if gpu_file:
        (tmp_path / "smart-turn-v3.2-gpu.onnx").touch()
    sessions, extracted, preloads = [], [], []

    class Extractor:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            return Extractor()

        def __call__(self, audio, **kwargs):
            extracted.append((audio, kwargs))
            return SimpleNamespace(input_features=np.zeros((1, 80, 800), dtype=np.float32))

    class Options:
        def add_session_config_entry(self, *args):
            pass

    class Session:
        def __init__(self, path, sess_options, providers):
            self.path = Path(path)
            self.cuda = self.path.name.endswith("gpu.onnx")
            self.runs = 0
            self.fallback_disabled = False
            sessions.append(self)

        def get_providers(self):
            return ["CUDAExecutionProvider", "CPUExecutionProvider"] if self.cuda and not silent_fallback else ["CPUExecutionProvider"]

        def get_inputs(self):
            return [SimpleNamespace(name="features")]

        def disable_fallback(self):
            self.fallback_disabled = True

        def run(self, _outputs, inputs, *args):
            assert inputs["features"].shape == (1, 80, 800)
            self.runs += 1
            if self.cuda and warmup_failure:
                raise RuntimeError("CUDA warmup failed")
            return [np.array([[0.8]])]

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=Options, InferenceSession=Session,
        get_available_providers=lambda:["CUDAExecutionProvider", "CPUExecutionProvider"] if available else ["CPUExecutionProvider"],
        preload_dlls=lambda:preloads.append(True), RunOptions=lambda:SimpleNamespace(terminate=False),
    ))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(WhisperFeatureExtractor=Extractor))
    monkeypatch.setattr("voice_terminal.compute.device_candidates", lambda *args:["cuda", "cpu"])
    model = SmartTurn(tmp_path, device="cuda")
    return model, sessions, extracted, preloads


def test_cuda_uses_gpu_weights_and_completes_real_warmup_before_ready(monkeypatch, tmp_path):
    model, sessions, _, preloads = backend(monkeypatch, tmp_path)
    assert model.device == "cuda"
    assert model.model_file == "smart-turn-v3.2-gpu.onnx"
    assert model.warmed and model.load_error is None
    assert sessions[0].runs == 2 and sessions[0].fallback_disabled
    assert preloads == [True]
    model.warmup()
    assert sessions[0].runs == 2


@pytest.mark.parametrize("failure", ["missing_library", "missing_weights", "silent_fallback", "warmup_failure"])
def test_gpu_failure_reloads_cpu_weights_and_reports_reason(monkeypatch, tmp_path, failure):
    model, sessions, _, _ = backend(monkeypatch, tmp_path,
        available=failure != "missing_library", gpu_file=failure != "missing_weights",
        silent_fallback=failure == "silent_fallback", warmup_failure=failure == "warmup_failure")
    assert model.requested_device == "cuda" and model.device == "cpu"
    assert model.model_file == sessions[-1].path.name == "smart-turn-v3.2-cpu.onnx"
    assert model.providers == ["CPUExecutionProvider"]
    assert model.warmed and sessions[-1].runs == 2
    assert "回退" in model.load_error


def test_preserves_trailing_eight_second_window_and_left_silence_padding(monkeypatch, tmp_path):
    model, _, extracted, _ = backend(monkeypatch, tmp_path)
    short = np.arange(160, dtype=np.int16)
    assert model(short.tobytes()) == {"probability": 0.8}
    audio, kwargs = extracted[-1]
    assert audio.shape == (128000,) and not audio[:-160].any()
    np.testing.assert_array_equal(audio[-160:], short / 32768)
    assert kwargs["do_normalize"] is True
    long = np.arange(160000, dtype=np.int16)
    model(long.tobytes())
    np.testing.assert_array_equal(extracted[-1][0], long[-128000:] / 32768)


def test_cancelled_request_never_enters_native_inference(monkeypatch, tmp_path):
    model, sessions, _, _ = backend(monkeypatch, tmp_path)
    options = model.new_run_options()
    options.terminate = True
    with pytest.raises(RuntimeError, match="cancelled"):
        model(b"\0\0", options)
    assert sessions[0].runs == 2


def test_optional_gpu_model_manifest_preserves_cpu_install_and_checks_gpu_checksum(tmp_path):
    import hashlib
    import json
    from voice_terminal.analysis.model_setup import MODELS, SMART_TURN_GPU, validate
    for name, (_, revision, filenames) in MODELS.items():
        folder = tmp_path / "analysis" / name
        folder.mkdir(parents=True)
        checksums = {}
        for filename in [*filenames, *(["preprocessor_config.json"] if name == "smart-turn" else [])]:
            payload = filename.encode()
            (folder / filename).write_bytes(payload)
            checksums[filename] = hashlib.sha256(payload).hexdigest()
        (folder / "manifest.json").write_text(json.dumps({"revision":revision, "sha256":checksums}), encoding="utf-8")
    assert validate(tmp_path) == []
    assert len(validate(tmp_path, smart_turn_gpu=True)) == 1
    folder = tmp_path / "analysis/smart-turn"
    manifest_file = folder / "manifest.json"
    record = json.loads(manifest_file.read_text())
    (folder / SMART_TURN_GPU).write_bytes(b"gpu")
    record["sha256"][SMART_TURN_GPU] = hashlib.sha256(b"gpu").hexdigest()
    manifest_file.write_text(json.dumps(record), encoding="utf-8")
    assert validate(tmp_path, smart_turn_gpu=True) == []
    (folder / SMART_TURN_GPU).write_bytes(b"tampered")
    assert "checksum mismatch" in validate(tmp_path)[0]


def test_lane_load_warmup_and_calls_share_one_thread_and_keep_request_timings():
    from voice_terminal.analysis.worker import AnalysisLane
    threads = []

    class Model:
        model_file = "smart-turn-v3.2-gpu.onnx"
        warmed = False

        def __init__(self):
            threads.append(threading.get_ident())

        def warmup(self):
            threads.append(threading.get_ident())
            self.warmed = True

        def __call__(self, pcm):
            threads.append(threading.get_ident())
            self.last_latency = {"preprocess_ms": 1.0, "model_ms": 2.0}
            return {"probability": 0.8}

    async def scenario():
        lane = AnalysisLane(model_name="SmartTurn")
        try:
            lane.prepare(Model)
            result, latency = await lane.run_measured(b"\0\0", "first")
            assert result == {"probability": 0.8}
            assert latency["preprocess_ms"] == 1.0 and latency["model_ms"] == 2.0
            assert latency["executor_wait_ms"] >= 0 and latency["native_ms"] >= 0
            assert len(threads) == 3 and len(set(threads)) == 1
            assert threads[0] != threading.get_ident()
            assert lane.health_payload()["model_warmed"] is True
            assert lane.health_payload()["model"] == Model.model_file
            lane.model.last_latency = {"model_ms": 200}
            assert latency["model_ms"] == 2.0
        finally:
            lane.close()
    asyncio.run(scenario())
