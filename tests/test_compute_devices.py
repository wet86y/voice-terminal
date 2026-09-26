from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice_terminal.compute import device_candidates, startup_device
from voice_terminal.config import COMPUTE_MODEL_IDS


class ComputeDeviceTests(unittest.TestCase):
    def hardware(self, *, supported: bool = True, free: int = 4 * 1024**3) -> dict:
        return {
            "gpu": {"available": True, "free_bytes": free, "total_bytes": 8 * 1024**3},
            "models": {
                name: {"gpu_available": supported, "reason": None}
                for name in COMPUTE_MODEL_IDS
            },
        }

    def test_auto_uses_gpu_only_when_backend_and_free_memory_are_available(self) -> None:
        with patch("voice_terminal.compute.hardware_status", return_value=self.hardware()):
            self.assertEqual(startup_device("auto", "sensevoice"), "cuda")
            self.assertEqual(device_candidates("auto", "sensevoice"), ["cuda", "cpu"])
        with patch("voice_terminal.compute.hardware_status", return_value=self.hardware(free=1)):
            self.assertEqual(startup_device("auto", "sensevoice"), "cpu")
        with patch("voice_terminal.compute.hardware_status", return_value=self.hardware(supported=False)):
            self.assertEqual(startup_device("auto", "sensevoice"), "cpu")
        self.assertEqual(device_candidates("cuda", "sensevoice", "cpu"), ["cuda", "cpu"])

    def test_sensevoice_cuda_load_failure_retries_on_the_previous_cpu_device(self) -> None:
        from voice_terminal.analysis.sensevoice import SenseVoice

        calls: list[str] = []

        def auto_model(**kwargs):
            device = kwargs["device"]
            calls.append(device)
            if device == "cuda":
                raise RuntimeError("simulated CUDA out-of-memory")
            return SimpleNamespace()

        with (
            patch("voice_terminal.compute.device_candidates", return_value=["cuda", "cpu"]),
            patch("funasr.AutoModel", side_effect=auto_model),
            patch("torch.cuda.empty_cache"),
        ):
            model = SenseVoice(
                Path("sensevoice-model"),
                Path("fsmn-vad-model"),
                device="cuda",
                fallback_device="cpu",
            )

        self.assertEqual(calls, ["cuda", "cpu", "cpu"])
        self.assertEqual(model.device, "cpu")
        self.assertEqual(model.internal_vad_device, "cpu")
        self.assertIn("CUDA", model.load_error or "")

    def test_analysis_health_reports_requested_and_actual_devices(self) -> None:
        from voice_terminal.analysis.worker import AnalysisLane

        model = SimpleNamespace(
            requested_device="cuda",
            device="cuda",
            internal_vad_device="cuda",
            load_error=None,
        )
        lane = AnalysisLane(model, model_name="SenseVoiceSmall")
        try:
            health = lane.health_payload()
        finally:
            lane.close()
        self.assertEqual(health["status"], "READY")
        self.assertEqual(health["requested_device"], "cuda")
        self.assertEqual(health["device"], "cuda")
        self.assertEqual(health["internal_vad_device"], "cuda")
        self.assertIsInstance(health["process_memory_rss_bytes"], int)


if __name__ == "__main__":
    unittest.main()
