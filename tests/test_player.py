from __future__ import annotations

import unittest

from voice_terminal.audio.player import PcmPlayer


class PcmPlayerTests(unittest.TestCase):
    def test_partial_chunk_is_played_before_following_chunk(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        first_chunk = b"\x01\x00\x02\x00\x03\x00"
        second_chunk = b"\x04\x00\x05\x00"
        player.enqueue("turn_1", first_chunk)
        player.enqueue("turn_1", second_chunk)

        first_output = bytearray(4)
        player._callback(first_output, 2, None, None)
        self.assertEqual(bytes(first_output), b"\x01\x00\x02\x00")

        second_output = bytearray(4)
        player._callback(second_output, 2, None, None)
        self.assertEqual(bytes(second_output), b"\x03\x00\x04\x00")

        third_output = bytearray(4)
        player._callback(third_output, 2, None, None)
        self.assertEqual(bytes(third_output), b"\x05\x00\x00\x00")
        self.assertEqual(player.buffered_ms, 0)

    def test_progress_counts_enqueued_and_callback_consumed_audio(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        player.allow("turn_1")
        player.enqueue("turn_1", b"\x01\x00" * 1000)

        before = player.progress("turn_1")
        self.assertEqual(before["enqueued_ms"], 1000)
        self.assertEqual(before["played_ms"], 0)
        self.assertEqual(before["buffered_ms"], 1000)

        player._callback(bytearray(1000), 500, None, None)
        after = player.progress("turn_1")
        self.assertEqual(after["enqueued_ms"], 1000)
        self.assertEqual(after["played_ms"], 500)
        self.assertEqual(after["buffered_ms"], 500)


if __name__ == "__main__":
    unittest.main()
