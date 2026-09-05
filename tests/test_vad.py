import unittest

import numpy as np

from voice_terminal.vad.detector import FsmnVadDetector


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


if __name__ == "__main__":
    unittest.main()
