import unittest
from pathlib import Path

from voice_terminal.asr.worker import AsrWorkerEngine


class AsrWorkerTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
