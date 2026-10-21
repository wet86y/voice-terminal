import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from voice_terminal.vad.detector import FsmnVadDetector
from voice_terminal.resources import ResourcePolicy


class _FakeVadModel:
    def __init__(self, responses=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses = list(responses or [[{"value": []}]])

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else [{"value": []}]


class VadTests(unittest.TestCase):
    @staticmethod
    def _detector(model: _FakeVadModel) -> FsmnVadDetector:
        detector = FsmnVadDetector.__new__(FsmnVadDetector)
        detector._np = np
        detector._model = model
        detector._cache = {}
        detector._active = False
        detector._model_chunk_ms = 200
        detector._feed_count = 0
        return detector

    def test_feed_uses_fun_asr_streaming_window_and_cache(self) -> None:
        model = _FakeVadModel()
        detector = self._detector(model)

        detector.feed(b"\x00\x00" * 160)

        call = model.calls[0]
        self.assertEqual(call["chunk_size"], 200)
        self.assertTrue(call["is_streaming_input"])
        self.assertIs(call["cache"], detector._cache)

    def test_empty_streaming_result_keeps_speech_active(self) -> None:
        model = _FakeVadModel(
            [
                [{"value": [[0, -1]]}],
                [{"value": []}],
                [{"value": [[-1, 600]]}],
            ]
        )
        detector = self._detector(model)

        started = detector.feed(b"\x00\x00" * 160)
        ongoing = detector.feed(b"\x00\x00" * 160)
        ended = detector.feed(b"\x00\x00" * 160)

        self.assertTrue(started.is_speech)
        self.assertTrue(started.speech_started)
        self.assertTrue(ongoing.is_speech)
        self.assertFalse(ongoing.speech_ended)
        self.assertFalse(ended.is_speech)
        self.assertTrue(ended.speech_ended)

    def test_auto_uses_onnx_when_cpu_asset_and_backend_are_available(self) -> None:
        with patch("voice_terminal.resources.ResourcePolicy.load",
                   return_value=ResourcePolicy({}, vad_backend="auto")), \
             patch("voice_terminal.vad.onnx_backend.OnnxStreamingModel", return_value=_FakeVadModel()) as onnx:
            detector = FsmnVadDetector(model_dir=Path("unused"), device="cpu")

        self.assertEqual(detector.backend, "onnx")
        onnx.assert_called_once()

    def test_auto_falls_back_to_funasr_when_onnx_dependency_is_missing(self) -> None:
        fallback_factory = lambda **_kwargs: _FakeVadModel()
        with patch("voice_terminal.resources.ResourcePolicy.load",
                   return_value=ResourcePolicy({}, vad_backend="auto")), \
             patch("voice_terminal.vad.onnx_backend.OnnxStreamingModel", side_effect=ImportError("not installed")), \
             patch("voice_terminal.asr.model_loader.auto_model_class", return_value=fallback_factory), \
             patch("voice_terminal.compute.device_candidates", return_value=["cpu"]):
            detector = FsmnVadDetector(model_dir=Path("unused"), device="cpu")

        self.assertEqual(detector.backend, "funasr")
        self.assertIn("ImportError", detector.load_error or "")

    def test_auto_keeps_gpu_request_on_funasr(self) -> None:
        fallback_factory = lambda **_kwargs: _FakeVadModel()
        with patch("voice_terminal.resources.ResourcePolicy.load",
                   return_value=ResourcePolicy({}, vad_backend="auto")), \
             patch("voice_terminal.vad.onnx_backend.OnnxStreamingModel") as onnx, \
             patch("voice_terminal.asr.model_loader.auto_model_class", return_value=fallback_factory), \
             patch("voice_terminal.compute.device_candidates", return_value=["cuda"]), \
             patch("voice_terminal.tensor_runtime.activate_tensor_runtime") as tensor_runtime:
            detector = FsmnVadDetector(model_dir=Path("unused"), device="cuda")

        self.assertEqual(detector.backend, "funasr")
        self.assertEqual(detector.device, "cuda")
        tensor_runtime.assert_called_once_with("cuda")
        onnx.assert_not_called()

    def test_explicit_funasr_policy_skips_onnx_even_on_cpu(self) -> None:
        fallback_factory = lambda **_kwargs: _FakeVadModel()
        with patch("voice_terminal.resources.ResourcePolicy.load",
                   return_value=ResourcePolicy({}, vad_backend="funasr")), \
             patch("voice_terminal.vad.onnx_backend.OnnxStreamingModel") as onnx, \
             patch("voice_terminal.asr.model_loader.auto_model_class", return_value=fallback_factory), \
             patch("voice_terminal.compute.device_candidates", return_value=["cpu"]):
            detector = FsmnVadDetector(model_dir=Path("unused"), device="cpu")

        self.assertEqual(detector.backend, "funasr")
        onnx.assert_not_called()


if __name__ == "__main__":
    unittest.main()
