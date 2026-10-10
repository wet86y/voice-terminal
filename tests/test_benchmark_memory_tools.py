from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semantic_output_extracts_sensevoice_tags():
    worker = _load_script("benchmark_runtime_resources_test", "benchmark_runtime_resources.py")
    result = worker._semantic_output("<|zh|><|NEUTRAL|><|Speech|>欢迎大家。")
    assert result == {
        "text": "欢迎大家。",
        "language": "zh",
        "emotion": "NEUTRAL",
        "events": ["Speech"],
        "raw_tags": ["zh", "NEUTRAL", "Speech"],
    }


def test_semantic_output_preserves_fun_asr_structured_tags():
    worker = _load_script("benchmark_runtime_resources_test", "benchmark_runtime_resources.py")
    result = worker._semantic_output({
        "text": "欢迎大家。", "language": "zh", "emotion": "NEUTRAL",
        "events": ["Speech"], "raw_tags": ["zh", "NEUTRAL", "Speech"],
        "no_speech": False, "complete": True,
    })
    assert result["text"] == "欢迎大家。"
    assert result["language"] == "zh"
    assert result["emotion"] == "NEUTRAL"
    assert result["events"] == ["Speech"]


def test_refinement_signature_compares_labels_across_backend_result_shapes():
    worker = _load_script("benchmark_runtime_resources_test", "benchmark_runtime_resources.py")
    funasr = {
        "text": "欢迎大家。", "segments": [{"text": "欢迎大家。", "language": "zh",
                                             "emotion": "NEUTRAL", "events": ["Speech"],
                                             "raw_tags": ["zh", "NEUTRAL", "Speech"]}],
    }
    onnx = ["<|zh|><|NEUTRAL|><|Speech|>欢迎大家。"]
    assert worker._semantic_signature(funasr, "refine") == worker._semantic_signature(onnx, "refine")


def test_comparison_keeps_unknown_metrics_unknown():
    orchestration = _load_script("benchmark_model_memory_test", "benchmark_model_memory.py")
    compared = orchestration._compare(
        {"resources": {"sampled_peaks": {"rss_bytes": 10, "gpu_shared_bytes": None}},
         "inference_ms": {"percentiles": {"p50": 1}}, "semantic_outputs": ["same"],
         "audio": {"sha256": "fixed"}},
        {"resources": {"sampled_peaks": {"rss_bytes": 7, "gpu_shared_bytes": None}},
         "inference_ms": {"percentiles": {"p50": 2}}, "semantic_outputs": ["same"],
         "audio": {"sha256": "fixed"}},
    )
    assert compared["rss_peak_bytes"]["candidate_minus_baseline"] == -3
    assert compared["shared_gpu_peak_bytes"]["candidate_minus_baseline"] is None
    assert compared["semantic_outputs_equal"] is True
    assert compared["audio_sha256_equal"] is True


def test_speaker_comparison_uses_allclose_and_distance_metrics():
    orchestration = _load_script("benchmark_model_memory_test", "benchmark_model_memory.py")
    compared = orchestration._compare_speaker_embeddings(
        [[1.0, 0.0]], [[1.0, 5e-7]], rtol=1e-5, atol=1e-6)
    assert compared["allclose"] is True
    assert compared["iterations"][0]["max_absolute_difference"] == 5e-7
    assert compared["iterations"][0]["cosine_distance"] is not None

def test_vad_validation_backend_override_is_temporary_and_fallback_is_rejected():
    support = _load_script("vad_validation_support_test", "vad_validation_support.py")

    from voice_terminal.resources import ResourcePolicy

    class PolicyAwareDetector:
        def __init__(self, *, model_dir, device, end_silence_ms):
            self.model_dir = model_dir
            self.device = device
            self.end_silence_ms = end_silence_ms
            self.backend = ResourcePolicy.load().vad_backend

    for backend in ("funasr", "onnx"):
        with patch("voice_terminal.vad.detector.FsmnVadDetector", PolicyAwareDetector):
            detector = support.build_vad_detector(
                model_dir=Path("unused"),
                expected_backend=backend,
                device="cpu",
                end_silence_ms=200,
            )
        assert detector.backend == backend
        assert detector.device == "cpu"

    class FallbackDetector:
        def __init__(self, **_kwargs):
            self.backend = "funasr"
            self.load_error = "requested ONNX asset is unavailable"

    with patch("voice_terminal.vad.detector.FsmnVadDetector", FallbackDetector):
        try:
            support.build_vad_detector(
                model_dir=Path("unused"),
                expected_backend="onnx",
                device="cpu",
            )
        except RuntimeError as exc:
            assert "backend mismatch" in str(exc)
            assert "funasr" in str(exc)
        else:
            raise AssertionError("backend fallback must not be reported as the requested candidate")

def test_vad_validation_scripts_forward_and_verify_requested_backend():
    from types import SimpleNamespace

    validator = _load_script("validate_vad_streaming_test", "validate_vad_streaming.py")
    worker = _load_script("benchmark_runtime_resources_vad_test", "benchmark_runtime_resources.py")

    class Sampler:
        def phase(self, *_args, **_kwargs):
            pass

    for backend in ("funasr", "onnx"):
        fake_detector = SimpleNamespace(backend=backend, device="cpu")
        with patch.object(validator, "build_vad_detector", return_value=fake_detector) as build:
            assert validator._build_detector(Path("unused"), backend) is fake_detector
            assert build.call_args.kwargs["expected_backend"] == backend

        arguments = SimpleNamespace(
            role="vad", backend=backend, device="cpu", threads=None,
            vad_dir=Path("unused"), model_root=Path("."),
        )
        with patch.object(worker, "build_vad_detector", return_value=fake_detector) as build:
            detector, _run = worker._build_model(arguments, None, b"", Sampler())
            assert detector is fake_detector
            assert build.call_args.kwargs["expected_backend"] == backend
