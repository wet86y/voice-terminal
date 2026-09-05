import unittest

from voice_terminal.stream.text_bridge import TextBridge


class TextBridgeTests(unittest.TestCase):
    def test_flushes_at_sentence_break(self) -> None:
        bridge = TextBridge(min_chars=4, max_chars=30, max_wait_ms=350)
        self.assertEqual(bridge.feed("这是第一句。"), ["这是第一句。"])
        self.assertEqual(bridge.finish(), [])

    def test_flushes_at_maximum_length(self) -> None:
        bridge = TextBridge(min_chars=4, max_chars=6, max_wait_ms=350)
        self.assertEqual(bridge.feed("abcdef"), ["abcdef"])

    def test_finish_flushes_short_tail(self) -> None:
        bridge = TextBridge(min_chars=8, max_chars=36, max_wait_ms=350)
        self.assertEqual(bridge.feed("短尾"), [])
        self.assertEqual(bridge.finish(), ["短尾"])


if __name__ == "__main__":
    unittest.main()
