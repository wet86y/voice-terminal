"""实时标点的跨块文本、结束刷新及信箱分句回归测试。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
import sys

import pytest

from voice_terminal.asr.punctuation import StreamingPunctuation
from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection
from voice_terminal.asr.protocol import AudioChunk
from voice_terminal.controller.transcript_segmenter import TranscriptSegmenter
from voice_terminal.config import AppConfig, AsrConfig


class _Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []
        self.model = SimpleNamespace(punc_list=["<unk>", "_", "，", "。", "？", "、"])

    def generate(self, *, input, cache, data_type):
        self.calls.append((input, cache, list(cache.get("pre_text", []))))
        cache["pre_text"] = cache.get("pre_text", []) + [input]
        row = next(self.outputs)
        if isinstance(row, Exception):
            raise row
        text, index = row
        return [{"text": text, "punc_array": [SimpleNamespace(item=lambda: index)]}]


def _punc(outputs):
    channel = StreamingPunctuation(Path("."))
    channel.model = _Model(outputs)
    return channel


@pytest.mark.parametrize("requested,actual", [("cpu", "cpu"), ("cuda", "cuda"), ("auto", "cpu")])
def test_punctuation_selects_its_own_tensor_packages_before_funasr_import(tmp_path, monkeypatch, requested, actual):
    """主识别的显卡偏好不能让处理器标点导入显卡构建；显式显卡标点仍有效。"""
    runtime = tmp_path / "runtime"
    overlay = runtime / "Lib/cpu-packages"
    for name in ("torch", "torchaudio"):
        (overlay / name).mkdir(parents=True)
        (overlay / name / "__init__.py").touch()
    (runtime / "runtime.json").write_text('{"gpu_profile":"cu128"}', encoding="utf-8")
    model = tmp_path / "punctuation"
    model.mkdir()
    (model / "config.yaml").write_text("model: CTTransformerStreaming\n", encoding="utf-8")
    for name in ("model.pt", "tokens.json"):
        (model / name).touch()
    monkeypatch.setattr(sys, "executable", str(runtime / "python.exe"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delenv("VOICE_GPU_FALLBACK", raising=False)
    monkeypatch.setenv("VOICE_TENSOR_DEVICE", "cuda")
    monkeypatch.setattr("voice_terminal.asr.punctuation.device_candidates", lambda *_: [actual])

    def import_model(_directory):
        assert (str(overlay) in sys.path) == (actual == "cpu")
        if actual == "cpu":
            assert sys.path[0] == str(overlay)
        monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda=None if actual == "cpu" else "12.8")))
        return lambda **kwargs: SimpleNamespace(kwargs=kwargs, generate=lambda **_: [])

    monkeypatch.setattr("voice_terminal.asr.punctuation.auto_model_class", import_model)
    channel = StreamingPunctuation(model, requested)
    channel.load()
    assert channel.warmed and channel.device == actual


class StreamingPunctuationTests(unittest.TestCase):
    def test_context_isolated_and_delayed_punctuation_is_appended_once(self):
        channel = _punc([("今天阳光很好", 1), ("，我们出去走走", 3), ("另一个人", 1)])
        first = channel.process("a", "今天阳光很好", final=False)
        second = channel.process("a", "我们出去走走", final=False)
        channel.process("b", "另一个人", final=False)
        self.assertEqual(first + second, "今天阳光很好，我们出去走走")
        self.assertEqual(channel.model.calls[1][2], ["今天阳光很好"])
        self.assertEqual(channel.model.calls[2][2], [])
        self.assertEqual(channel.process("a", "", final=True), "。")
        self.assertEqual(len(channel.model.calls), 3)
        old_cache = channel.model.calls[0][1]
        channel.clear_turn("a")
        self.assertEqual(old_cache, {})
        self.assertNotIn("a", channel._turns)

    def test_final_prediction_and_rule_finish_never_duplicate_words(self):
        channel = _punc([("你明天来吗", 4), ("你好", 1), ("好", 1), ("好", 1)])
        self.assertEqual(channel.process("question", "你明天来吗", final=True), "你明天来吗？")
        self.assertEqual(channel.process("hello", "你好", final=True), "你好。")
        self.assertEqual(channel.process("repeat", "好", final=False) +
                         channel.process("repeat", "好", final=True), "好好。")

    def test_inference_failure_preserves_prefix_and_disables_only_failed_turn(self):
        channel = _punc([("今天很好", 1), RuntimeError("推理失败"), ("其他轮次", 1)])
        self.assertEqual(channel.process("a", "今天很好", final=False), "今天很好")
        self.assertEqual(channel.process("a", "出去走走", final=False), "出去走走")
        self.assertEqual(channel.process("a", "然后回家", final=True), "然后回家")
        self.assertEqual(channel.process("b", "其他轮次", final=True), "其他轮次。")
        self.assertEqual(len(channel.model.calls), 3)
        self.assertEqual(channel._turns["a"].cache, {})

    def test_repeated_context_in_response_falls_back_without_losing_delta(self):
        channel = _punc([("第一句话", 1), ("第一句话第二句话", 1)])
        self.assertEqual(channel.process("a", "第一句话", final=False), "第一句话")
        self.assertEqual(channel.process("a", "第二句话", final=True), "第二句话")
        self.assertTrue(channel._turns["a"].failed)

    def test_missing_or_offline_model_is_classified_and_raw_text_survives(self):
        channel = StreamingPunctuation(Path("missing-realtime-model"))
        channel.load()
        self.assertEqual(channel.status()["status"], "DEGRADED")
        self.assertEqual(channel.process("a", "原文", final=True), "原文")
        self.assertFalse(channel._turns)

    def test_defaults_select_realtime_model_and_cpu(self):
        self.assertTrue(AsrConfig().punctuation_enabled)
        self.assertEqual(AsrConfig().punctuation_device, "cpu")
        self.assertEqual(AppConfig().punctuation_model_dir.name,
                         "punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727")
        with self.assertRaises(ValueError):
            AsrConfig(punctuation_device="invalid")

    def test_offline_model_is_not_loaded_as_realtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.yaml").write_text("model: CTTransformer\n", encoding="utf-8")
            for name in ("model.pt", "tokens.json"):
                (root / name).touch()
            channel = StreamingPunctuation(root)
            channel.load()
            self.assertIsNone(channel.model)
            self.assertIn("CTTransformerStreaming", channel.load_error)


class StreamingPunctuationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_punctuation_before_releasing_its_cache(self):
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
        engine.punctuation = _punc([("原始文字", 1)])
        engine._infer_sync = lambda *_: "原始文字"
        entered, release = threading.Event(), threading.Event()
        generate = engine.punctuation.model.generate
        def slow_generate(**kwargs):
            entered.set()
            release.wait(timeout=3)
            return generate(**kwargs)
        engine.punctuation.model.generate = slow_generate
        task = asyncio.create_task(engine.infer("cancel", b"\x00" * engine.chunk_stride_bytes, False))
        try:
            await asyncio.to_thread(entered.wait, 1)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(engine.punctuation._turns)
            self.assertFalse(engine._transcript_by_turn)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            engine.close()

    async def test_turn_finish_and_disconnect_both_release_punctuation_context(self):
        for explicit_finish in (False, True):
            engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
            channel = _punc([("识别增量", 1)])
            engine.punctuation = channel
            engine._infer_sync = lambda *_: "识别增量"
            class Socket:
                def __aiter__(self):
                    async def messages():
                        yield json.dumps(AudioChunk("a", b"\x00" * engine.chunk_stride_bytes).to_dict())
                        if explicit_finish:
                            yield json.dumps({"type": "turn.finish", "turn_id": "a"})
                    return messages()
                async def send(self, raw):
                    self.response = json.loads(raw)
            try:
                socket = Socket()
                await handle_asr_connection(socket, engine)
                self.assertEqual(socket.response["text"], "识别增量")
                self.assertFalse(channel._turns)
                self.assertEqual(channel.model.calls[0][1], {})
            finally:
                engine.close()

    async def test_each_window_and_audio_span_are_punctuated_before_mailbox_segmentation(self):
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
        channel = _punc([("今天天气很好", 1), ("，我们去散步", 4)])
        engine.punctuation = channel
        asr = iter(["今天天气很好", "我们去散步", ""])
        engine._infer_sync = lambda *_: next(asr)
        try:
            first = await engine.infer("a", b"\x00" * engine.chunk_stride_bytes, False)
            second = await engine.infer("a", b"\x00" * engine.chunk_stride_bytes, False)
            self.assertEqual(second.text, "今天天气很好，我们去散步")
            self.assertEqual([call[0] for call in channel.model.calls], ["今天天气很好", "我们去散步"])
            self.assertEqual(second.segments[0]["text_delta"], "，我们去散步")
            # 少于20字即由跨块逗号形成第一条信箱分句；识别尚未结束。
            segmenter = TranscriptSegmenter()
            self.assertIsNone(segmenter.feed(first.text))
            segmented = segmenter.feed(second.text)
            self.assertEqual(segmented["segments"], ["今天天气很好，"])
            final = await engine.infer("a", b"", True)
            self.assertEqual(final.text, "今天天气很好，我们去散步？")
            self.assertEqual(final.segments[0]["text_delta"], "？")
            self.assertEqual("".join(span["text_delta"] for result in (first, second, final)
                                      for span in result.segments), final.text)
            self.assertGreaterEqual(final.latency["punctuation_ms"], 0)
            self.assertFalse(channel._turns)
            repeated = await engine.infer("a", b"", True)
            self.assertEqual(repeated.text, "")
            self.assertEqual(len(channel.model.calls), 2)
        finally:
            engine.close()

    async def test_batched_audio_still_punctuates_per_window(self):
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", chunk_size=(0, 1, 0))
        engine.punctuation = _punc([("打开", 1), ("浏览器", 3)])
        asr = iter(["打开", "浏览器"])
        engine._infer_sync = lambda *_: next(asr)
        try:
            result = await engine.infer("a", b"\x00" * engine.chunk_stride_bytes * 2, True)
            self.assertEqual(result.text, "打开浏览器。")
            self.assertEqual(len(engine.punctuation.model.calls), 2)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
