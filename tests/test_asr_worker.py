import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from pathlib import Path

import pytest

from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection


class AsrWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_inference_does_not_wait_for_the_default_executor(self) -> None:
        """默认线程池被占满时，识别仍在专用线程完成。"""
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")
        executor = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(executor)
        release = threading.Event()
        blocker = executor.submit(release.wait)
        engine._infer_sync = lambda *_: "识别完成"
        try:
            result = await asyncio.wait_for(engine.infer("dedicated", b"\x00" * engine.chunk_stride_bytes, True), 1)
            self.assertEqual(result.text, "识别完成")
            self.assertEqual(result.latency["model_calls"], 1)
            self.assertGreaterEqual(result.latency["executor_wait_ms"], 0)
        finally:
            release.set()
            blocker.result(timeout=1)
            engine.close()

    async def test_cancellation_waits_for_native_completion_before_clearing_cache(self) -> None:
        """取消不能让原生线程在清理后重新创建轮次缓存。"""
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")
        entered, release = threading.Event(), threading.Event()
        class StreamingChild:
            alive = True
            def __init__(self): self.cache_by_turn = {}
            def clear_turn(self, turn_id): self.cache_by_turn.pop(turn_id, None)
            def close(self): self.alive = False
        child = engine._model = StreamingChild()
        def native(turn_id, *_):
            entered.set()
            release.wait(timeout=2)
            child.cache_by_turn[turn_id] = {"finished": True}
            return "完成"
        engine._infer_sync = native
        task = asyncio.create_task(engine.infer("cancel", b"\x00" * engine.chunk_stride_bytes, False))
        try:
            await asyncio.to_thread(entered.wait, 1)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn("cancel", child.cache_by_turn)
            self.assertNotIn("cancel", engine._pending_audio_by_turn)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            engine.close()

    async def test_empty_final_flushes_cached_tail_after_exact_windows(self) -> None:
        for window_count in (1, 3):
            with self.subTest(window_count=window_count):
                engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")
                calls: list[tuple[bytes, bool]] = []

                def fake_infer(turn_id: str, pcm: bytes, is_final: bool) -> str:
                    calls.append((pcm, is_final))
                    return "浏览器" if is_final else "打开" if len(calls) == 1 else ""

                engine._infer_sync = fake_infer  # type: ignore[method-assign]
                audio = b"\x01\x00" * (engine.chunk_stride_samples * window_count)
                partial = await engine.infer("turn_exact", audio, False)
                final = await engine.infer("turn_exact", b"", True)

                self.assertEqual(partial.text, "打开")
                self.assertEqual(final.text, "打开浏览器")
                self.assertTrue(final.emitted)
                self.assertEqual(b"".join(pcm for pcm, _ in calls), audio)
                self.assertEqual([is_final for _, is_final in calls], [False] * window_count + [True])
                self.assertEqual(calls[-1][0], b"")
                self.assertNotIn("turn_exact", engine._pending_audio_by_turn)
                self.assertNotIn("turn_exact", engine._model_call_count_by_turn)

                # 重复结束信号不会再刷新或生成重复文字，也不会创建新模型调用。
                repeated = await engine.infer("turn_exact", b"", True)
                self.assertEqual(repeated.text, "")
                self.assertEqual(len(calls), window_count + 1)

    async def test_empty_turn_never_calls_model(self) -> None:
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")

        def unexpected_infer(turn_id: str, pcm: bytes, is_final: bool) -> str:
            self.fail("空轮次不应启动模型推理")

        engine._infer_sync = unexpected_infer  # type: ignore[method-assign]
        await engine.infer("turn_empty", b"", False)
        final = await engine.infer("turn_empty", b"", True)
        self.assertEqual(final.text, "")
        self.assertFalse(final.emitted)

    async def test_final_falls_back_to_latest_partial_text(self) -> None:
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
        outputs = iter(["打开浏览器", ""])
        engine._infer_sync = lambda turn_id, pcm, is_final: next(outputs)  # type: ignore[method-assign]

        partial = await engine.infer("turn_1", b"\x00" * engine.chunk_stride_bytes, False)
        final = await engine.infer("turn_1", b"\x00\x00", True)

        self.assertEqual(partial.text, "打开浏览器")
        self.assertTrue(partial.emitted)
        self.assertEqual(final.text, "打开浏览器")
        self.assertFalse(final.emitted)

    async def test_streaming_windows_are_aggregated_and_text_is_cumulative(self) -> None:
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
        outputs = iter(["打开", "浏览器"])
        calls: list[tuple[int, bool]] = []

        def fake_infer(turn_id: str, pcm: bytes, is_final: bool) -> str:
            calls.append((len(pcm), is_final))
            return next(outputs)

        engine._infer_sync = fake_infer  # type: ignore[method-assign]

        # Two small microphone frames make one FunASR model window.  The
        # first frame alone must not invoke generate().
        first = await engine.infer("turn_2", b"\x00" * (engine.chunk_stride_bytes // 2), False)
        partial = await engine.infer("turn_2", b"\x00" * (engine.chunk_stride_bytes // 2), False)
        final = await engine.infer("turn_2", b"\x00" * 640, True)

        self.assertEqual(first.text, "")
        self.assertFalse(first.emitted)
        self.assertEqual(partial.text, "打开")
        self.assertTrue(partial.emitted)
        self.assertEqual(final.text, "打开浏览器")
        self.assertTrue(final.emitted)
        self.assertEqual(
            calls,
            [(engine.chunk_stride_bytes, False), (640, True)],
        )


@pytest.mark.asyncio
async def test_compute_device_rebuild_exits_old_model_child_before_spawning_target(tmp_path, monkeypatch):
    events = []

    class Child:
        def __init__(self, name, requested, device):
            self.name, self.device = name, device
            self.requested_device = requested
            self.warmed = True
            self.load_error = None
            self.runtime = "PyTorch（推理框架）"
            self.resource_status = {"rss_bytes": 100}
            self.resource_footprint = {"rss_bytes": 50}
            self.pid = 101 if name == "old" else 202
            self.alive = True
        def close(self):
            self.alive = False
            events.append(f"exit:{self.name}")

    old = Child("old", "cpu", "cpu")
    engine = AsrWorkerEngine(model_dir=tmp_path, device="cpu")
    engine._model = old
    engine.model_warmed = True

    def process(kind, model_dir, **options):
        assert kind == "streaming"
        assert events[-1].startswith("exit:")
        requested = options["device"]
        if requested == "cuda":
            assert options["require_cuda"] is True
        actual = "cpu" if requested == "cpu" else "cuda"
        events.append(f"start:{requested}")
        return Child(requested, requested, actual)

    monkeypatch.setattr("voice_terminal.model_process.ModelProcess", process)
    try:
        status = await engine.configure_device("cuda")
        assert events == ["exit:old", "start:cuda"]
        assert status["device"] == "cuda" and status["model_warmed"] is True
        assert engine.requested_device == "cuda" and engine.device == "cuda"
        assert engine.fallback_device == "cuda"
        status = await engine.configure_device("cpu")
        assert events[-2:] == ["exit:cuda", "start:cpu"]
        assert status["device"] == "cpu" and engine.device == "cpu"
        assert engine.fallback_device == "cpu"
        status = await engine.configure_device("auto")
        assert events[-2:] == ["exit:cpu", "start:auto"]
        assert status["device"] == "cuda" and engine.requested_device == "auto"
    finally:
        engine.close()
    assert events[-1] == "exit:auto"


@pytest.mark.asyncio
async def test_paraformer_device_rebuild_switches_enabled_offline_lane_to_same_device(tmp_path, monkeypatch):
    events = []

    class Child:
        def __init__(self, name, device):
            self.name, self.device = name, device
            self.requested_device = device
            self.warmed = True
            self.load_error = None
            self.resource_status = {"rss_bytes": 100}
            self.resource_footprint = {"rss_bytes": 50}
            self.pid = 101 if name == "old" else 202
            self.alive = True
        def close(self):
            self.alive = False
            events.append(f"exit:{self.name}")

    class Offline:
        enabled = True
        device = "cpu"
        async def configure(self, enabled, device, **_options):
            events.append(f"offline:{'start' if enabled else 'stop'}:{device}")
            self.enabled = enabled
            self.device = device if enabled else None
        def status(self):
            return {"status": "READY" if self.enabled else "DISABLED", "model_loaded": self.enabled,
                    "model_warmed": self.enabled, "device": self.device}
        def close(self):
            self.enabled = False

    engine = AsrWorkerEngine(model_dir=tmp_path, device="cpu")
    engine._model = Child("old", "cpu")
    engine.offline = Offline()
    engine.dual_pass_enabled = True

    def process(kind, _model_dir, **options):
        assert kind == "streaming"
        assert events[-1] == "exit:old"
        events.append(f"start:{options['device']}")
        return Child(options["device"], "cuda")

    monkeypatch.setattr("voice_terminal.model_process.ModelProcess", process)
    try:
        status = await engine.configure_device("cuda")
        assert status["device"] == "cuda"
        assert status["offline"]["device"] == "cuda"
        assert events == ["offline:stop:cpu", "exit:old", "start:cuda", "offline:start:cuda"]
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_paraformer_device_rebuild_does_not_start_disabled_offline_lane(tmp_path, monkeypatch):
    events = []

    class Child:
        def __init__(self, name, device):
            self.name, self.device, self.requested_device = name, device, device
            self.warmed, self.load_error, self.alive = True, None, True
            self.resource_status, self.resource_footprint = {"rss_bytes": 100}, {"rss_bytes": 50}
            self.pid = 101 if name == "old" else 202
        def close(self):
            self.alive = False
            events.append(f"exit:{self.name}")

    class Offline:
        enabled = False
        async def configure(self, *_args, **_kwargs):
            raise AssertionError("disabled offline lane must stay unloaded")
        def status(self):
            return {"status": "DISABLED", "model_loaded": False, "model_warmed": False}
        def close(self):
            pass

    engine = AsrWorkerEngine(model_dir=tmp_path, device="cpu")
    engine._model = Child("old", "cpu")
    engine.offline = Offline()
    engine.dual_pass_enabled = False

    def process(kind, _model_dir, **options):
        assert kind == "streaming" and events[-1] == "exit:old"
        events.append(f"start:{options['device']}")
        return Child(options["device"], "cpu")

    monkeypatch.setattr("voice_terminal.model_process.ModelProcess", process)
    try:
        status = await engine.configure_device("cpu")
        assert status["offline"]["status"] == "DISABLED"
        assert events == ["exit:old", "start:cpu"]
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_health_during_device_switch_is_nonblocking_and_does_not_start_a_second_model(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    starts = []

    class Child:
        def __init__(self, device):
            self.device = device
            self.requested_device = device
            self.warmed = True
            self.load_error = None
            self.runtime = "PyTorch（推理框架）"
            self.resource_status = {"rss_bytes": 100}
            self.resource_footprint = {"rss_bytes": 50}
            self.tensor_cuda_build = device == "cuda"
            self.alive = True
            self.pid = 202 if device == "cuda" else 101

        def close(self):
            self.alive = False

    class Socket:
        remote_address = None

        def __init__(self):
            self.messages = [json.dumps({"type": "health", "request_id": "health-during-switch"})]
            self.responses = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

        async def send(self, value):
            self.responses.append(json.loads(value))

    engine = AsrWorkerEngine(model_dir=tmp_path, device="cpu")
    engine._model = Child("cpu")
    engine.device = "cpu"
    engine.model_warmed = True

    def create_process(_kind, _model_dir, **options):
        starts.append(options["device"])
        entered.set()
        assert release.wait(3), "test did not release the target model load"
        return Child(options["device"])

    monkeypatch.setattr("voice_terminal.model_process.ModelProcess", create_process)
    switch = asyncio.create_task(engine.configure_device("cuda"))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set(), "target model load did not start"

        socket = Socket()
        await asyncio.wait_for(handle_asr_connection(socket, engine), timeout=1)

        assert socket.responses[0]["status"] == "STARTING"
        assert socket.responses[0]["model_loaded"] is False
        assert starts == ["cuda"]
        release.set()
        status = await asyncio.wait_for(switch, timeout=2)
        assert status["device"] == "cuda"
        assert starts == ["cuda"]
    finally:
        release.set()
        if not switch.done():
            await asyncio.wait_for(switch, timeout=2)
        engine.close()


if __name__ == "__main__":
    unittest.main()
