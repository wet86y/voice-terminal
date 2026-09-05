import unittest
from pathlib import Path

from voice_terminal.asr.worker import AsrWorkerEngine


class AsrWorkerTests(unittest.IsolatedAsyncioTestCase):
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
