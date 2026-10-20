from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from voice_terminal.compute import device_candidates, startup_device, loaded_funasr_device
from voice_terminal.config import COMPUTE_MODEL_IDS


class ComputeDeviceTests(unittest.TestCase):
    def test_loaded_device_uses_resolved_loader_options(self) -> None:
        """加载器静默回退或指定显卡编号时，报告运行结果而非请求设备。"""
        self.assertEqual(loaded_funasr_device(SimpleNamespace(kwargs={"device": "cpu"}), "cuda"), "cpu")
        self.assertEqual(loaded_funasr_device(SimpleNamespace(kwargs={"device": "cuda:0"}), "cuda"), "cuda")
        self.assertEqual(loaded_funasr_device(SimpleNamespace(), "cpu"), "cpu")

    def test_asr_silent_cpu_fallback_keeps_request_and_reports_actual_device(self) -> None:
        from voice_terminal.asr.worker import AsrWorkerEngine

        engine = AsrWorkerEngine(model_dir=Path("."), device="cuda")
        with (
            patch("voice_terminal.compute.startup_device", return_value="cuda"),
            patch("funasr.AutoModel", return_value=SimpleNamespace(kwargs={"device": "cpu"}, generate=lambda **_: [])),
        ):
            engine.load()
        self.assertEqual(engine.requested_device, "cuda")
        self.assertEqual(engine.device, "cpu")
        self.assertTrue(engine.model_loaded)
        self.assertIn("CPU", engine.load_error or "")
        self.assertTrue(engine.model_warmed)
        engine.close()

    def test_asr_gpu_warmup_failure_falls_back_before_becoming_ready(self) -> None:
        """权重加载成功但 CUDA（图形处理器加速）真实推理失败时回退。"""
        from voice_terminal.asr.worker import AsrWorkerEngine
        engine = AsrWorkerEngine(model_dir=Path("."), device="cuda")
        expected_warmup_samples = engine.chunk_stride_samples
        calls = []
        def factory(**options):
            device = options["device"]
            calls.append(device)
            def generate(**kwargs):
                if device == "cuda":
                    raise RuntimeError("CUDA kernel unavailable")
                self.assertEqual(len(kwargs["input"]), 0 if kwargs["is_final"] else expected_warmup_samples)
                return []
            return SimpleNamespace(kwargs={"device": device}, generate=generate)
        try:
            with (patch("voice_terminal.compute.startup_device", return_value="cuda"),
                  patch("funasr.AutoModel", side_effect=factory), patch("torch.cuda.empty_cache")):
                engine.load()
            self.assertEqual(calls, ["cuda", "cpu"])
            self.assertEqual(engine.device, "cpu")
            self.assertTrue(engine.model_warmed)
            self.assertIn("kernel unavailable", engine.load_error)
            self.assertEqual(engine._cache_by_turn, {})
            self.assertEqual(engine._model_call_count_by_turn, {})
        finally:
            engine.close()

    def test_sensevoice_and_internal_vad_report_their_own_resolved_device(self) -> None:
        from voice_terminal.analysis.sensevoice import SenseVoice

        with (
            patch("voice_terminal.compute.device_candidates", return_value=["cuda", "cpu"]),
            patch("funasr.AutoModel", side_effect=[
                SimpleNamespace(kwargs={"device": "cuda:0"}),
                SimpleNamespace(kwargs={"device": "cpu"}),
            ]) as factory,
        ):
            model = SenseVoice(Path("sensevoice-model"), Path("vad-model"), device="cuda")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(model.requested_device, "cuda")
        self.assertEqual(model.device, "cuda")
        self.assertEqual(model.internal_vad_device, "cpu")
        self.assertIn("自动回退", model.load_error or "")

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
            self.assertEqual(startup_device("auto", "emotion"), "cuda")
            self.assertEqual(device_candidates("auto", "emotion"), ["cuda", "cpu"])
        with patch("voice_terminal.compute.hardware_status", return_value=self.hardware(free=1)):
            self.assertEqual(startup_device("auto", "emotion"), "cpu")
        with patch("voice_terminal.compute.hardware_status", return_value=self.hardware(supported=False)):
            self.assertEqual(startup_device("auto", "emotion"), "cpu")
        self.assertEqual(device_candidates("cuda", "emotion", "cpu"), ["cuda", "cpu"])

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


class ComputeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_speaker_compute_status_keeps_runtime_faults_despite_ready_model_health(self):
        """模型仍加载时，声纹通信或推理故障仍投影为不可用，并保留原因。"""
        from voice_terminal.config import AppConfig
        from voice_terminal.core.state import RuntimeState
        from voice_terminal.server.websocket import SessionRuntime

        for client_state in ("degraded", "disconnected", "missing", "ready"):
            with self.subTest(client_state=client_state):
                runtime = SessionRuntime.__new__(SessionRuntime)
                config = AppConfig()
                runtime.config = replace(config, speaker=replace(config.speaker, mode="observe"))
                runtime.state = RuntimeState()
                runtime.state.set_service("speaker", "READY")
                runtime.asr = None
                runtime._compute_models = {}
                runtime.startup_errors = []
                runtime.speaker_profile = SimpleNamespace(snapshot=lambda: {"available": True})
                runtime._speaker_test_result = {}
                runtime.speaker = None if client_state == "missing" else SimpleNamespace(
                    connected=client_state != "disconnected", degraded=client_state == "degraded",
                    last_error="声纹推理请求超时" if client_state == "degraded" else None,
                    health=AsyncMock(return_value={"status": "READY", "model_loaded": True,
                        "device": "cpu", "runtime": "ONNX Runtime（推理运行时）"}),
                )
                hardware = {"models": {name: {"gpu_available": False} for name in COMPUTE_MODEL_IDS}}
                analysis = SimpleNamespace(health=AsyncMock(return_value={
                    "refine": {"status": "DISABLED"}, "endpoint": {"status": "DISABLED"},
                }))
                with (patch("voice_terminal.server.websocket.hardware_status", return_value=hardware),
                      patch("voice_terminal.server.websocket.AnalysisClient", return_value=analysis)):
                    result = (await runtime.compute_status())["models"]["campplus"]
                self.assertEqual(result["service"], "READY" if client_state == "ready" else "ERROR")
                if client_state == "ready":
                    self.assertIsNone(result["runtime_error"])
                else:
                    self.assertTrue(result["runtime_error"])
                    self.assertFalse(runtime.speaker_status()["filter_allowed"])
                if client_state == "degraded":
                    self.assertEqual(result["runtime_error"], "声纹推理请求超时")

    async def test_idle_asr_health_uses_a_separate_connection_and_closes_it(self):
        """停止监听关闭会话连接后，模型仍可通过只读连接正确显示。"""
        from voice_terminal.server.websocket import SessionRuntime

        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.asr = SimpleNamespace(connected=False, url="ws://127.0.0.1:1/asr", health=AsyncMock())
        status = {"status": "READY", "model_loaded": True, "device": "cuda"}
        probe = SimpleNamespace(connect=AsyncMock(), health=AsyncMock(return_value=status), close=AsyncMock())
        with patch("voice_terminal.server.websocket.AsrWorkerClient", return_value=probe) as factory:
            self.assertEqual(await runtime._asr_compute_health(), status)
        factory.assert_called_once_with(runtime.asr.url)
        probe.connect.assert_awaited_once()
        probe.health.assert_awaited_once_with(timeout_s=2.0)
        probe.close.assert_awaited_once()
        self.assertFalse(runtime.asr.connected)
        runtime.asr.health.assert_not_awaited()

    async def test_active_asr_health_reuses_the_session_connection(self):
        """监听时复用会话健康请求，不额外创建连接或关闭活动输入。"""
        from voice_terminal.server.websocket import SessionRuntime

        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.asr = SimpleNamespace(connected=True, health=AsyncMock(return_value={"status": "READY"}))
        with patch("voice_terminal.server.websocket.AsrWorkerClient") as factory:
            self.assertEqual(await runtime._asr_compute_health(), {"status": "READY"})
        factory.assert_not_called()
        runtime.asr.health.assert_awaited_once_with(timeout_s=2.0)

    async def test_failed_idle_probe_is_closed_and_keeps_the_real_error(self):
        """连接与健康检查失败均清理临时连接，真实故障不伪装为就绪。"""
        from voice_terminal.server.websocket import SessionRuntime

        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.asr = SimpleNamespace(connected=False, url="ws://127.0.0.1:1/asr")
        for operation in ("connect", "health"):
            with self.subTest(operation=operation):
                probe = SimpleNamespace(connect=AsyncMock(), health=AsyncMock(), close=AsyncMock())
                getattr(probe, operation).side_effect = RuntimeError("识别服务未就绪")
                with patch("voice_terminal.server.websocket.AsrWorkerClient", return_value=probe):
                    with self.assertRaisesRegex(RuntimeError, "识别服务未就绪"):
                        await runtime._asr_compute_health()
                probe.close.assert_awaited_once()
                self.assertFalse(runtime.asr.connected)


if __name__ == "__main__":
    unittest.main()
